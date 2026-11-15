"""Phase 2b pilot: the REAL Geofabrik download + pyosmium filter for one place, measured.

Runs inside the production worker image. Writes NOTHING to MongoDB Atlas: leads go into an
in-memory mongomock database, only to count unique leads and duplicates.

    # from the repository root (PowerShell / Git Bash); use a disk with ~5 GB free for /data and /tmp
    docker build -t lead-worker:prod ./worker
    docker run --rm -u root \
      -v <pilot-folder>/data:/data -v <pilot-folder>/tmp:/tmp/leadfinder-osm \
      -v <repo>/worker/scripts:/app/scripts:ro \
      lead-worker:prod sh -c "pip install -q mongomock==4.3.0 && PYTHONPATH=/app python scripts/pilot_extract.py country:GB"

Measures: download size + time, filtering time, peak RAM, peak temporary disk, filtered cache size,
gym elements, unique leads (after the area filter + deduplication), OSM-tag website/email/phone
counts, repeat-search time (cache hit) and the refresh behaviour. Website crawling (email/phone found
on websites) is NOT part of this pilot: it needs a real database and hours of polite crawling.
"""

from __future__ import annotations

import json
import os
import resource
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import mongomock
from bson import ObjectId

from app import leads as leads_mod
from app.geo.boundaries import Boundaries
from app.osm import extracts as ex
from app.osm import mapping

PLACES = {
    "country:GB": {"_id": "country:GB", "kind": "country", "name": "United Kingdom", "countryCode": "GB",
                   "countryName": "United Kingdom"},
    "gn:2643743": {"_id": "gn:2643743", "kind": "city", "name": "London", "countryCode": "GB",
                   "countryName": "United Kingdom", "admin1Name": "England", "lat": 51.50853, "lon": -0.12574,
                   "population": 8961989},
}


def dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


class PeakDisk(threading.Thread):
    def __init__(self, path: Path):
        super().__init__(daemon=True)
        self.path, self.peak, self.stop = path, 0, threading.Event()

    def run(self):
        while not self.stop.wait(0.5):
            try:
                self.peak = max(self.peak, dir_bytes(self.path))
            except OSError:
                pass


def count_leads(db, area, pin, cache) -> dict:
    owner, job = ObjectId(), ObjectId()
    s = dict.fromkeys(("elements", "outsideArea", "duplicates", "discovered", "newLeads", "updated", "merged",
                       "withWebsite", "withEmailTag", "withPhoneTag"), 0)
    seen = set()
    for _, el in ex.read_elements(cache.abs(pin.file)):
        s["elements"] += 1
        pt = el if "lat" in el else el.get("center") or {}
        if "lat" in pt and not area.contains(float(pt["lat"]), float(pt["lon"])):
            s["outsideArea"] += 1
            continue
        lead = mapping.element_to_lead(el, now=ex.utcnow(), area_country_code=area.country_code)
        if lead is None:
            continue
        if lead["externalIds"]["osm"] in seen:
            s["duplicates"] += 1
            continue
        seen.add(lead["externalIds"]["osm"])
        s["discovered"] += 1
        s["withWebsite"] += "website" in lead
        s["withEmailTag"] += bool(lead.get("emails"))
        s["withPhoneTag"] += bool(lead.get("phones"))
        r = leads_mod.upsert_lead(db.leads, owner_id=owner, job_id=job, lead=lead, now=ex.utcnow())
        s[{"new": "newLeads", "merged": "merged"}.get(r, "updated")] += 1
    s["uniqueLeadsInDb"] = db.leads.count_documents({"ownerId": owner})
    return s


def main(place_id: str) -> None:
    cfg = ex.ExtractConfig(cache_dir=Path(os.getenv("OSM_CACHE_DIR", "/data/osm")),
                           tmp_dir=Path(os.getenv("OSM_TMP_DIR", "/tmp/leadfinder-osm")),
                           user_agent="LeadFinderBot/0.3 (pilot; single download)")
    db = mongomock.MongoClient(tz_aware=True).pilot
    downloads = []
    client = ex.make_http_client(cfg)
    client.event_hooks["request"].append(
        lambda r: downloads.append(str(r.url)) if r.method == "GET" and str(r.url).endswith(".osm.pbf") else None)
    cache = ex.ExtractCache(db, cfg, owner="pilot", client=client)
    area = Boundaries.from_pinned().resolve(PLACES[place_id])
    index = ex.GeofabrikIndex.load(client, cfg)
    sizes = {}
    regions = index.plan(area.geometry, size_of=lambda r: sizes.setdefault(r.id, ex.resolve_source(client, r).length),
                         max_download_bytes=cfg.max_download_bytes, max_job_download_bytes=cfg.max_job_download_bytes)
    report: dict = {"place": area.display_name, "boundary": area.boundary,
                    "extracts": [{"id": r.id, "bytes": sizes.get(r.id)} for r in regions]}

    stages = []
    tmp_peak = PeakDisk(cfg.tmp_dir)
    tmp_peak.start()
    t0 = time.monotonic()
    pins = cache.ensure(regions, on_stage=lambda s: stages.append((round(time.monotonic() - t0, 1), s)))
    t_ready = time.monotonic() - t0
    tmp_peak.stop.set()
    tmp_peak.join()
    dl = next((t for t, s in stages if s.startswith("Downloading")), None)
    prep = next((t for t, s in stages if s.startswith("Preparing")), None)
    report["firstSearch"] = {
        "totalSeconds": round(t_ready, 1),
        "downloadSeconds": round(prep - dl, 1) if dl is not None and prep is not None else None,
        "filterSeconds": round(t_ready - prep, 1) if prep is not None else None,
        "peakRssMB": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
        "peakTempDiskMB": round(tmp_peak.peak / 2**20),
        "tempDiskAfterMB": round(dir_bytes(cfg.tmp_dir) / 2**20),
        "pbfDownloads": len(downloads),
        "stages": stages,
    }
    for p in pins:
        doc = db.osmExtracts.find_one({"_id": p.region_id})["current"]
        report.setdefault("cache", []).append({
            "region": p.region_id, "version": p.version, "osmTimestamp": p.osm_timestamp,
            "sourceBytes": doc["sourceBytes"], "sourceMd5Verified": bool(doc["sourceMd5"]),
            "filteredBytes": doc["bytes"], "gymElements": doc["features"]})
        report.setdefault("leads", []).append({"region": p.region_id, **count_leads(db, area, p, cache)})
        cache.unpin(p)

    t1 = time.monotonic()
    again = cache.ensure(regions)
    report["repeatSearch"] = {"secondsToData": round(time.monotonic() - t1, 2), "newDownloads": len(downloads) - 1
                              if len(downloads) else 0}
    for p in again:
        cache.unpin(p)

    for r in regions:  # a week later: served immediately, refreshed in the background
        db.osmExtracts.update_one({"_id": r.id}, {"$set": {"current.refreshedAt": ex.utcnow() - timedelta(days=8)}})
    t2 = time.monotonic()
    stale = cache.ensure(regions)
    served = round(time.monotonic() - t2, 2)
    for p in stale:
        cache.unpin(p)
    n_before = len(downloads)
    t3 = time.monotonic()
    refreshed = cache.refresh_requested(index)
    report["staleSearch"] = {"secondsToData": served, "backgroundRefreshes": refreshed,
                             "refreshSeconds": round(time.monotonic() - t3, 1),
                             "downloadsDuringRefresh": len(downloads) - n_before,
                             "note": "0 downloads = Geofabrik had no newer version; the cache was only marked fresh"}
    report["volumeMB"] = round(dir_bytes(cfg.cache_dir) / 2**20, 1)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "country:GB")
