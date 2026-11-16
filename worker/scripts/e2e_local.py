"""Phase 3 end-to-end run: the REAL worker (main.process -> run_job) against a throwaway LOCAL MongoDB,
with the real cached data from the pilots: a Geofabrik GB extract (OSM, already filtered) and Overture
release tiles. Measures and checks the whole path: search area -> OSM + Overture lanes -> MongoDB ->
website check -> contact verification, plus repeat searches, user isolation, concurrent jobs, a killed
worker (resume from checkpoint), SIGTERM hand-back and Cancel.

Nothing leaves this machine:
  - MongoDB must be local (refuses MongoDB Atlas); the database E2E_DB is dropped at the start.
  - The worker's HTTP client is a local fake: the only request the planner makes for cached data (a
    Geofabrik HEAD for the size of an alternative extract) is answered locally; anything else FAILS the run.
  - Websites are played by a local fake (real crawling of thousands of gyms is not a test); DNS (MX + the
    SSRF guard) is stubbed. The crawler code itself (robots, pages, extraction, checks, merge) is real.

    # from the repository root; mongod on the host at port 27099; data from the pilots
    docker run --rm -u root --add-host=host.docker.internal:host-gateway \
      -e E2E_MONGO_URI=mongodb://host.docker.internal:27099 \
      -v <pilot-gb>/data/osm:/seed/osm:ro -v <pilot-ov>/data/overture:/seed/overture:ro \
      -v <work>:/e2e -v <repo>/worker/scripts:/app/scripts:ro \
      lead-worker:prod sh -c "PYTHONPATH=/app python scripts/e2e_local.py"
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from zlib import crc32

import httpx
import pymongo
from bson import ObjectId
from pymongo import monitoring

from app import jobs as q
from app import main
from app.config import Settings
from app.osm.extracts import sha256_file
from app.pipeline import run_job

URI = os.getenv("E2E_MONGO_URI", "mongodb://127.0.0.1:27099")
DB = os.getenv("E2E_DB", "e2e_phase3")
WORK = Path(os.getenv("E2E_WORK", "/e2e"))
SEED_OSM = Path(os.getenv("E2E_SEED_OSM", "/seed/osm"))
SEED_OV = Path(os.getenv("E2E_SEED_OVERTURE", "/seed/overture"))
PILOT_DB = os.getenv("E2E_PILOT_DB", "pilot_overture")  # holds the Overture tile documents of the pilot
RELEASE = "2026-10-21.0"

LONDON = {"_id": "gn:2643743", "kind": "city", "name": "London", "countryCode": "GB", "countryName": "United Kingdom",
          "admin1Name": "England", "lat": 51.50853, "lon": -0.12574, "population": 8961989}
MANCHESTER = {"_id": "gn:2643123", "kind": "city", "name": "Manchester", "countryCode": "GB",
              "countryName": "United Kingdom", "admin1Name": "England", "lat": 53.48095, "lon": -2.23743,
              "population": 568996}
CATEGORIES = ["gym", "boxing_club", "mma_academy", "martial_arts_school", "wrestling_club", "fitness_center"]

assert "mongodb.net" not in URI, "the end-to-end run must never use MongoDB Atlas"


# ---------------------------------------------------------------- measurement

class Ops(monitoring.CommandListener):
    """Counts MongoDB commands (round trips) and their server time, per scenario."""

    WRITES = {"insert", "update", "delete", "findAndModify"}

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.counts, self.ms, self.coll_writes, self.pending = Counter(), 0.0, Counter(), {}

    def started(self, event):
        if event.command_name in self.WRITES:
            with self.lock:
                self.pending[event.request_id] = str(event.command.get(event.command_name))

    def succeeded(self, event):
        with self.lock:
            self.counts[event.command_name] += 1
            self.ms += event.duration_micros / 1000
            coll = self.pending.pop(event.request_id, None)
            if coll:
                self.coll_writes[coll] += 1

    def failed(self, event):
        self.succeeded(event)

    def snapshot(self) -> dict:
        with self.lock:
            c = {k: v for k, v in self.counts.items() if k not in ("hello", "isMaster", "ping", "endSessions")}
            return {"roundTrips": sum(c.values()), "writes": sum(v for k, v in c.items() if k in self.WRITES),
                    "byCommand": dict(sorted(c.items(), key=lambda kv: -kv[1])), "serverMs": round(self.ms),
                    "writesByCollection": dict(self.coll_writes.most_common())}


def rss_mb() -> float:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


class Peak:
    def __init__(self):
        self.peak, self._stop = rss_mb(), threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while not self._stop.wait(0.05):
            self.peak = max(self.peak, rss_mb())

    def stop(self) -> int:
        self._stop.set()
        self.t.join()
        return round(max(self.peak, rss_mb()))


# ---------------------------------------------------------------- local fakes (no network)

NET_LOG: list[str] = []


def worker_http(request: httpx.Request) -> httpx.Response:
    """The worker's HTTP client. Cached data needs at most a HEAD for the size of an uncached extract."""
    NET_LOG.append(f"{request.method} {request.url}")
    if request.url.host == "download.geofabrik.de" and request.method == "HEAD":
        return httpx.Response(200, headers={"content-length": str(150 * 1024**2), "etag": '"e2e"'})
    if request.url.host == "download.geofabrik.de" and str(request.url).endswith(".md5"):
        return httpx.Response(404)
    raise httpx.ConnectError(f"e2e: unexpected network request {request.method} {request.url}")


def site_kind(host: str) -> int:
    return crc32(host.encode()) % 20


def web(request: httpx.Request) -> httpx.Response:
    """Plays every business website: most answer with a contact page, some are down or block us."""
    host, path = request.url.host, request.url.path
    k = site_kind(host)
    if path == "/robots.txt":
        return httpx.Response(200, text="User-agent: LeadFinderBot\nDisallow: /\n") if k == 1 else httpx.Response(404)
    if k == 0:
        return httpx.Response(503)  # down
    html = {"content-type": "text/html; charset=utf-8"}
    num = crc32(host.encode()) % 10000
    if path in ("", "/"):
        return httpx.Response(200, headers=html, text=(
            f"<html><body><h1>{host}</h1><a href='/contact'>Contact us</a> <a href='/privacy'>Privacy</a>"
            f"<p>Call 020 7946 {num:04d}</p><a href='https://www.instagram.com/{host.split('.')[0]}'>IG</a></body></html>"))
    if path == "/contact":
        local = "info" if k % 2 else "hello"
        return httpx.Response(200, headers=html, text=(
            f"<html><body><a href='mailto:{local}@{host.removeprefix('www.')}'>Email us</a>"
            f"<a href='https://wa.me/4479460{num:05d}'>WhatsApp</a></body></html>"))
    if path == "/privacy":
        return httpx.Response(200, headers=html, text="<html><body>dpo@privacy-firm.test</body></html>")
    return httpx.Response(404)


async def mx(domain: str):
    return crc32(domain.encode()) % 7 != 0  # ~1 in 7 email domains has no mail server


async def host_ips(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]  # a public address: the SSRF guard lets it through


# ---------------------------------------------------------------- setup

def settings() -> Settings:
    return Settings(
        mongodb_uri=URI, mongodb_db=DB, worker_id="e2e-worker", poll_interval=1, stale_after=600, max_attempts=3,
        crawler_contact_url="https://leadfinder.local/bot",
        osm_cache_dir=str(WORK / "osm"), osm_tmp_dir=str(WORK / "tmp-osm"),
        overture_cache_dir=str(WORK / "overture"), overture_release=RELEASE,
        crawl_delay=0, crawl_concurrency=20,
    )


def seed(client: pymongo.MongoClient):
    client.drop_database(DB)
    if WORK.exists():
        shutil.rmtree(WORK / "osm", ignore_errors=True)
        shutil.rmtree(WORK / "overture", ignore_errors=True)
    db = client[DB]
    create_indexes(db)
    # OpenStreetMap: the filtered GB extract from the Phase 2b pilot, published as the current version.
    src = next((SEED_OSM / "files" / "united-kingdom").glob("*.jsonl.gz"))
    rel = f"files/united-kingdom/{src.name}"
    dst = WORK / "osm" / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    (WORK / "osm" / "index").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SEED_OSM / "index" / "index-v1.json", WORK / "osm" / "index" / "index-v1.json")
    import gzip

    with gzip.open(dst, "rt", encoding="utf-8") as f:
        features = sum(1 for _ in f)
    now = q.now()
    db.osmExtracts.insert_one({
        "_id": "united-kingdom", "status": "ready", "readers": [], "refreshLease": None, "lastAccessAt": now,
        "current": {"version": src.name.rsplit("-", 1)[0], "file": rel, "sha256": sha256_file(dst),
                    "bytes": dst.stat().st_size, "features": features, "osmTimestamp": None,
                    "createdAt": now, "refreshedAt": now},
        "previous": None,
    })
    # Overture: the release tiles of the Phase 2c pilot (files + their documents).
    shutil.copytree(SEED_OV / "files", WORK / "overture" / "files")
    pilot = client[PILOT_DB]
    tiles = [{**d, "readers": [], "lease": None} for d in pilot.overtureTiles.find()]
    db.overtureTiles.insert_many(tiles)
    db.overtureReleases.insert_one({"_id": "latest", "release": RELEASE, "checkedAt": now})
    return {"osmFeatures": features, "osmFileMB": round(dst.stat().st_size / 1e6, 2), "overtureTiles": len(tiles)}


def create_indexes(db):
    """The production lead/job indexes (same as web/src/models; checked against Lead.ts in test_query_plans)."""
    A, D = pymongo.ASCENDING, pymongo.DESCENDING
    partial = lambda f: {"partialFilterExpression": {f: {"$type": "string"}}}  # noqa: E731
    for keys, opts in [
        ([("ownerId", A), ("externalIds.osm", A)], {"unique": True, **partial("externalIds.osm")}),
        ([("ownerId", A), ("externalIds.overture", A)], {"unique": True, **partial("externalIds.overture")}),
        ([("ownerId", A), ("externalIds.googlePlaceId", A)], {"unique": True, **partial("externalIds.googlePlaceId")}),
        ([("ownerId", A), ("dedupeKey", A)], {"unique": True, **partial("dedupeKey")}),
        ([("ownerId", A), ("externalIds.osmAliases", A)], {}),
        ([("ownerId", A), ("externalIds.overtureAliases", A)], {}),
        ([("ownerId", A), ("externalIds.overtureSources", A)], {}),
        ([("ownerId", A), ("dataLane", A), ("discoveredAt", D), ("_id", D)], {}),
        ([("ownerId", A), ("website.domain", A)], {}),
        ([("ownerId", A), ("phones.e164", A)], {}),
        ([("ownerId", A), ("nameNormalized", A), ("_id", A)], {}),
        ([("ownerId", A), ("discoveredAt", D), ("_id", D)], {}),
        ([("ownerId", A), ("jobIds", A)], {}),
        ([("geo", "2dsphere")], {}),
    ]:
        db.leads.create_index(keys, **opts)
    db.scrapingJobs.create_index([("status", A), ("createdAt", A)])
    db.scrapingJobs.create_index([("ownerId", A), ("createdAt", D)])
    db.suppressionList.create_index([("ownerId", A), ("type", A), ("valueHash", A)], unique=True)


def new_job(db, owner, place, sources=("osm", "overture")) -> ObjectId:
    return db.scrapingJobs.insert_one({
        "ownerId": owner, "location": {"query": place["name"], "place": dict(place)}, "categories": CATEGORIES,
        "sources": list(sources), "status": "queued", "attempts": 0, "progress": {"stage": "queued", "pct": 0},
        "stats": {"errors": 0}, "errorLog": [], "cancelRequested": False, "createdAt": q.now(),
    }).inserted_id


def runner(s: Settings):
    """main.process calls run_job(db, job, settings, stop=...): ours adds only the local fakes."""
    client = httpx.Client(transport=httpx.MockTransport(worker_http))

    def run(db, job, settings_, *, stop=None):
        return run_job(db, job, settings_, client=client, web_transport=httpx.MockTransport(web), mx_resolve=mx,
                       host_resolve=host_ips, stop=stop)

    return run


def claim(db, job_id, worker="e2e-worker"):
    doc = q.claim_next_job(db.scrapingJobs, worker)
    assert doc and doc["_id"] == job_id, (doc and doc["_id"], job_id)
    return doc


def run_one(db, s, job_id, ops, *, stop=None) -> dict:
    main.run_job = runner(s)
    ops.reset()
    peak = Peak()
    t0 = time.monotonic()
    main.process(db, claim(db, job_id), s, stop=stop or (lambda: False))
    secs = time.monotonic() - t0
    job = db.scrapingJobs.find_one({"_id": job_id})
    return {"status": job["status"], "seconds": round(secs, 1), "peakRssMB": peak.stop(), "db": ops.snapshot(),
            "stats": {k: v for k, v in (job.get("stats") or {}).items() if k != "overture"},
            "overtureStats": (job.get("stats") or {}).get("overture"), "errors": len(job.get("errorLog") or []),
            "stage": job["progress"]["stage"]}


def lane_counts(db, owner) -> dict:
    out = {}
    for lane, f in (("osm", {"dataLane": {"$in": [None, "osm"]}}), ("overture", {"dataLane": "overture"})):
        base = {"ownerId": owner, **f}
        out[lane] = {
            "leads": db.leads.count_documents(base),
            "withWebsite": db.leads.count_documents({**base, "website.url": {"$exists": True}}),
            "withEmail": db.leads.count_documents({**base, "emails.0": {"$exists": True}}),
            "withPhone": db.leads.count_documents({**base, "phones.0": {"$exists": True}}),
            "noContact": db.leads.count_documents({**base, "contactStatus": "none"}),
            "contactStatus": dict(Counter(d.get("contactStatus") for d in db.leads.find(base, {"contactStatus": 1}))),
        }
    return out


def integrity(db, killed_jobs=()) -> dict:
    """Every invariant the system promises, over ALL leads of ALL users. Each value must be 0."""
    L = db.leads
    dup = lambda field: sum(1 for _ in L.aggregate([  # noqa: E731
        {"$match": {field: {"$type": "string"}}}, {"$group": {"_id": {"o": "$ownerId", "v": f"${field}"}, "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}}]))
    jobs = {j["_id"]: j["ownerId"] for j in db.scrapingJobs.find({}, {"ownerId": 1})}
    cross_owner = sum(1 for d in L.find({}, {"ownerId": 1, "jobIds": 1}) if any(jobs.get(j) != d["ownerId"] for j in d.get("jobIds", [])))
    return {
        "duplicateOsmIdsPerUser": dup("externalIds.osm"),
        "duplicateGersIdsPerUser": dup("externalIds.overture"),
        "duplicateDedupeKeysPerUser": dup("dedupeKey"),
        "overtureLeadsWithOsmIds": L.count_documents({"dataLane": "overture", "$or": [
            {"externalIds.osm": {"$exists": True}}, {"externalIds.osmAliases.0": {"$exists": True}}]}),
        "osmLeadsWithOvertureIds": L.count_documents({"dataLane": {"$ne": "overture"}, "$or": [
            {"externalIds.overture": {"$exists": True}}, {"externalIds.overtureAliases.0": {"$exists": True}}]}),
        "overtureLeadsWithOsmValues": L.count_documents({"dataLane": "overture", "$or": [
            {"emails.sourceType": "osm"}, {"phones.sourceType": "osm"}, {"fieldSources.name.sourceType": "osm"}]}),
        "osmLeadsWithOvertureValues": L.count_documents({"dataLane": {"$ne": "overture"}, "$or": [
            {"emails.sourceType": "overture"}, {"phones.sourceType": "overture"}, {"fieldSources.name.sourceType": "overture"}]}),
        "mergesAcrossLanes": L.count_documents({"mergeLog": {"$elemMatch": {"osm": {"$exists": True}, "overture": {"$exists": True}}}}),
        "leadsLinkedToAnotherUsersJob": cross_owner,
        "googleDataStored": L.count_documents({"$or": [{"externalIds.googlePlaceId": {"$exists": True}},
                                                       {"sources.type": "google"}, {"emails.sourceType": "google"}]}),
        "verifiedWithoutAPerson": L.count_documents({"contactStatus": "verified"}),
        "contactsWithoutChecks": L.count_documents({"contactsVersion": 2, "$or": [
            {"emails": {"$elemMatch": {"checks": {"$exists": False}}}}, {"phones": {"$elemMatch": {"checks": {"$exists": False}}}}]}),
        "valuesWithoutSource": L.count_documents({"$or": [
            {"emails": {"$elemMatch": {"sourceUrl": {"$exists": False}}}}, {"phones": {"$elemMatch": {"sourceUrl": {"$exists": False}}}}]}),
        "staleJobLeases": db.scrapingJobs.count_documents({"status": {"$ne": "running"}, "lease": {"$exists": True}}),
        # A SIGKILLed worker can't unpin: its pins expire by themselves (reader lease); they are not counted.
        "pinnedFilesLeft": sum(1 for c in (db.osmExtracts, db.overtureTiles) for d in c.find({"readers.0": {"$exists": True}})
                               for r in d["readers"] if not any(str(j) in r.get("owner", "") for j in killed_jobs)),
    }


def leftover_pins(db) -> list:
    return [{"file": d["_id"], "owner": r.get("owner"), "expires": r.get("until")}
            for c in (db.osmExtracts, db.overtureTiles) for d in c.find({"readers.0": {"$exists": True}}) for r in d["readers"]]


def chains(db, owner, top=5) -> dict:
    """Branch separation: the biggest chains stay one lead per branch (a merged chain would be 1)."""
    out = {}
    for lane, f in (("osm", {"dataLane": {"$in": [None, "osm"]}}), ("overture", {"dataLane": "overture"})):
        out[lane] = [(d["_id"], d["n"]) for d in db.leads.aggregate([
            {"$match": {"ownerId": owner, **f}}, {"$group": {"_id": "$nameNormalized", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}}, {"$limit": top}])]
    out["mostMergedLead"] = max((len(d.get("mergeLog") or []) for d in db.leads.find({"ownerId": owner}, {"mergeLog": 1})), default=0)
    return out


def lead_set(db, owner) -> set:
    return {(d.get("dataLane") or "osm", (d.get("externalIds") or {}).get("osm") or (d.get("externalIds") or {}).get("overture"))
            for d in db.leads.find({"ownerId": owner}, {"dataLane": 1, "externalIds": 1})}


# ---------------------------------------------------------------- scenarios

def child(job_id: str):
    """Subprocess for the kill test: claims the job and runs it until the parent kills it (SIGKILL)."""
    client = pymongo.MongoClient(URI, tz_aware=True)
    db, s = client[DB], settings()
    main.run_job = runner(s)
    main.process(db, claim(db, ObjectId(job_id), "doomed-worker"), s)


def main_run():
    ops = Ops()
    client = pymongo.MongoClient(URI, tz_aware=True, event_listeners=[ops])
    db, s = client[DB], settings()
    report: dict = {"seed": seed(client)}
    A, B, C, D, E, F = (ObjectId() for _ in range(6))

    print("S1 first search: London, OSM + Overture, user A", flush=True)
    j1 = new_job(db, A, LONDON)
    report["s1_firstSearch"] = {**run_one(db, s, j1, ops), "lanes": lane_counts(db, A), "chains": chains(db, A)}
    leads_a = lead_set(db, A)

    print("S2 repeat search: same user, same place", flush=True)
    before = db.leads.count_documents({"ownerId": A})
    j2 = new_job(db, A, LONDON)
    r2 = run_one(db, s, j2, ops)
    report["s2_repeatSearch"] = {**r2, "leadsBefore": before, "leadsAfter": db.leads.count_documents({"ownerId": A}),
                                 "sameLeadSet": lead_set(db, A) == leads_a}

    print("S3 another user, same search (isolation)", flush=True)
    j3 = new_job(db, B, LONDON)
    r3 = run_one(db, s, j3, ops)
    report["s3_otherUser"] = {**r3, "sameLeadsAsA": lead_set(db, B) == leads_a,
                              "userAUnchanged": db.leads.count_documents({"ownerId": A}) == before}

    print("S4 two workers at the same time (Manchester for C, London for D)", flush=True)
    j4a, j4b = new_job(db, C, MANCHESTER), new_job(db, D, LONDON)
    main.run_job = runner(s)
    ops.reset()
    peak, t0, errs = Peak(), time.monotonic(), []

    def worker(name):
        try:
            c2 = pymongo.MongoClient(URI, tz_aware=True, event_listeners=[ops])
            job = q.claim_next_job(c2[DB].scrapingJobs, name)
            main.process(c2[DB], job, s, stop=lambda: False)
        except Exception as exc:  # noqa: BLE001
            errs.append(repr(exc))

    ts = [threading.Thread(target=worker, args=(f"e2e-w{i}",)) for i in (1, 2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    report["s4_concurrent"] = {
        "seconds": round(time.monotonic() - t0, 1), "peakRssMB": peak.stop(), "db": ops.snapshot(), "errors": errs,
        "statuses": [db.scrapingJobs.find_one({"_id": j})["status"] for j in (j4a, j4b)],
        "manchesterLanes": lane_counts(db, C), "londonSameAsA": lead_set(db, D) == leads_a,
    }

    print("S5 worker killed (SIGKILL) during the job, recovered, resumed", flush=True)
    j5 = new_job(db, E, LONDON)
    proc = subprocess.Popen([sys.executable, __file__, "child", str(j5)], env={**os.environ, "PYTHONPATH": "/app"})
    t0 = time.monotonic()
    killed_at = None
    while proc.poll() is None and time.monotonic() - t0 < 900:
        n_ov = db.leads.count_documents({"ownerId": E, "dataLane": "overture"})
        if n_ov >= 2000:  # in the middle of the Overture stage
            proc.send_signal(signal.SIGKILL)
            proc.wait()
            killed_at = {"overtureLeads": n_ov, "osmLeads": db.leads.count_documents({"ownerId": E, "dataLane": {"$ne": "overture"}}),
                         "checkpoint": db.scrapingJobs.find_one({"_id": j5})["checkpoint"].get("overture", {}).get("position")}
            break
        time.sleep(0.2)
    assert killed_at, "the child finished before it could be killed"
    db.scrapingJobs.update_one({"_id": j5}, {"$set": {"lockedAt": q.now() - timedelta(hours=1)}})  # 10+ min later
    rec = q.recover_stale_jobs(db.scrapingJobs, 600, 3)
    db.scrapingJobs.update_one({"_id": j5}, {"$set": {"nextAttemptAt": q.now()}})  # skip the backoff wait
    r5 = run_one(db, s, j5, ops)
    report["s5_killAndResume"] = {**r5, "killedAt": killed_at, "recovered": rec, "sameLeadsAsA": lead_set(db, E) == leads_a,
                                  "attempts": db.scrapingJobs.find_one({"_id": j5})["attempts"]}

    print("S6 SIGTERM (redeploy) mid-job: handed back, resumed by the next worker", flush=True)
    j6 = new_job(db, F, LONDON)
    stop_when = lambda: db.leads.count_documents({"ownerId": F}) >= 1500  # noqa: E731
    first = run_one(db, s, j6, ops, stop=stop_when)
    mid = db.scrapingJobs.find_one({"_id": j6})
    second = run_one(db, s, j6, ops)
    report["s6_sigterm"] = {"first": {k: first[k] for k in ("status", "seconds", "stage")},
                            "attemptsAfterHandBack": mid["attempts"], "checkpointKept": bool(mid.get("checkpoint")),
                            "second": {k: second[k] for k in ("status", "seconds")},
                            "attemptsAtEnd": db.scrapingJobs.find_one({"_id": j6})["attempts"],
                            "sameLeadsAsA": lead_set(db, F) == leads_a}

    print("S7 Cancel during the job", flush=True)
    G = ObjectId()
    j7 = new_job(db, G, LONDON, sources=("overture",))
    cancel_when = lambda: (db.leads.count_documents({"ownerId": G}) >= 1000 and  # noqa: E731
                           db.scrapingJobs.update_one({"_id": j7}, {"$set": {"cancelRequested": True}}) and False)
    r7 = run_one(db, s, j7, ops, stop=cancel_when)
    report["s7_cancel"] = {k: r7[k] for k in ("status", "seconds", "stage")} | {"leadsKept": db.leads.count_documents({"ownerId": G})}

    if os.getenv("E2E_GB", "1") == "1":
        from app import pipeline

        timed: dict = {}
        real_stage = pipeline._overture_stage

        def stage(*a, **kw):
            t = time.monotonic()
            try:
                return real_stage(*a, **kw)
            finally:
                timed["overtureStageSeconds"] = round(time.monotonic() - t, 1)

        pipeline._overture_stage = stage
        for key, interval in (("s8_gbOverture", 60), ("s9_gbOverture_oldRenewEvery25Rows", 0)):
            print(f"{key}: Great Britain, Overture only (pins renewed every {interval or 'batch of 25 rows'} s)", flush=True)
            pipeline.RENEW_PINS_SECONDS = interval
            owner = ObjectId()
            j = new_job(db, owner, {"_id": "country:GB", "kind": "country", "name": "United Kingdom", "countryCode": "GB",
                                    "countryName": "United Kingdom"}, sources=("overture",))
            r = run_one(db, s, j, ops)
            report[key] = {**{k: r[k] for k in ("status", "seconds", "peakRssMB", "errors")}, **timed,
                           "overtureTileWrites": r["db"]["writesByCollection"].get("overtureTiles", 0),
                           "jobWrites": r["db"]["writesByCollection"].get("scrapingJobs", 0),
                           "leadWrites": r["db"]["writesByCollection"].get("leads", 0), "roundTrips": r["db"]["roundTrips"],
                           "overtureStats": r["overtureStats"], "websitesChecked": r["stats"].get("websitesChecked"),
                           "leads": db.leads.count_documents({"ownerId": owner}), "chains": chains(db, owner)}
        pipeline.RENEW_PINS_SECONDS = 60
        pipeline._overture_stage = real_stage

    report["leftoverPins"] = leftover_pins(db)
    report["integrity"] = integrity(db, killed_jobs=[j5])
    report["network"] = {"requests": len(NET_LOG), "distinct": sorted(set(NET_LOG))[:10]}
    report["dbSizeMB"] = round(db.command("dbStats")["dataSize"] / 1e6, 1)
    report["cacheMB"] = {k: round(sum(p.stat().st_size for p in (WORK / k).rglob("*") if p.is_file()) / 1e6, 1)
                         for k in ("osm", "overture")}
    print(json.dumps(report, indent=2, default=str))
    bad = {k: v for k, v in report["integrity"].items() if v}
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "child":
        child(sys.argv[2])
    else:
        main_run()
