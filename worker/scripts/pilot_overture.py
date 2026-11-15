"""Phase 2c pilot: the REAL Overture Maps query (S3 GeoParquet via DuckDB) for one place, measured.

Runs inside the production worker image; writes NOTHING to MongoDB Atlas. Default: in-memory mongomock
(no indexes, so large areas get slow: every upsert scans all leads). For realistic timings set
PILOT_MONGO_URI to a THROWAWAY local mongod (e.g. `docker run -d --name pilot-mongo mongo:7` on a docker
network); the pilot drops and re-creates its `pilot_overture` database with the production lead indexes.

    docker build -t lead-worker:prod ./worker
    docker run --rm -u root -e PYTHONPATH=/app \
      -v <pilot-folder>/data:/data -v <pilot-folder>/tmp:/tmp/leadfinder-osm \
      -v <repo>/worker/scripts:/app/scripts:ro \
      lead-worker:prod sh -c "pip install -q mongomock==4.3.0 && python scripts/pilot_overture.py gn:2643743"

Measures: release, tiles, query time, peak RAM, cache size, gym places, unique leads, duplicates merged
within Overture, website/email/phone fields present, repeat-search time (cache hit).
"""

from __future__ import annotations

import json
import os
import resource
import sys
import time
from pathlib import Path

import httpx
import mongomock
import pymongo
from bson import ObjectId

from app import leads as leads_mod
from app.geo.boundaries import Boundaries
from app.overture import mapping as ovm
from app.overture import places as ov

BATCH = int(os.getenv("PILOT_BATCH", "500"))  # same as pipeline.OVERTURE_WRITE_BATCH

PLACES = {
    "gn:2643743": {"_id": "gn:2643743", "kind": "city", "name": "London", "countryCode": "GB",
                   "countryName": "United Kingdom", "admin1Name": "England", "lat": 51.50853, "lon": -0.12574,
                   "population": 8961989},
    "country:GB": {"_id": "country:GB", "kind": "country", "name": "United Kingdom", "countryCode": "GB",
                   "countryName": "United Kingdom"},
}


def pilot_db():
    uri = os.getenv("PILOT_MONGO_URI", "")
    if not uri:
        return mongomock.MongoClient(tz_aware=True).pilot
    assert "mongodb.net" not in uri, "the pilot must never use MongoDB Atlas"
    client = pymongo.MongoClient(uri, tz_aware=True)
    client.drop_database("pilot_overture")
    db = client.pilot_overture
    A, partial = pymongo.ASCENDING, lambda f: {"partialFilterExpression": {f: {"$type": "string"}}}
    for keys, opts in [
        ([("ownerId", A), ("externalIds.overture", A)], {"unique": True, **partial("externalIds.overture")}),
        ([("ownerId", A), ("dedupeKey", A)], {"unique": True, **partial("dedupeKey")}),
        ([("ownerId", A), ("externalIds.overtureAliases", A)], {}),
        ([("ownerId", A), ("externalIds.overtureSources", A)], {}),
        ([("ownerId", A), ("website.domain", A)], {}),
        ([("ownerId", A), ("phones.e164", A)], {}),
        ([("ownerId", A), ("nameNormalized", A)], {}),
        ([("geo", "2dsphere")], {}),
    ]:
        db.leads.create_index(keys, **opts)
    return db


def main(place_id: str) -> None:
    cfg = ov.OvertureConfig(cache_dir=Path(os.getenv("OVERTURE_CACHE_DIR", "/data/overture")),
                            tmp_dir=Path(os.getenv("OSM_TMP_DIR", "/tmp/leadfinder-osm")) / "duckdb")
    db = pilot_db()
    client = httpx.Client(timeout=30, headers={"User-Agent": "LeadFinderBot/0.3 (pilot)"})
    area = Boundaries.from_pinned().resolve(PLACES[place_id])
    release = ov.current_release(db, client, cfg)
    tiles = ov.tiles_for(area.geometry, cfg.max_tiles_per_job)
    queries = []
    real = ov.query_duckdb(cfg)

    def counted(rel, bbox, beat):
        queries.append(bbox)
        yield from real(rel, bbox, beat)

    cache = ov.TileCache(db, cfg, owner="pilot", query_fn=counted)
    t0 = time.monotonic()
    pins = cache.ensure(release, tiles)
    first = time.monotonic() - t0
    owner, job = ObjectId(), ObjectId()
    s = dict.fromkeys(("rows", "outsideArea", "noCategory", "discovered", "newLeads", "updated", "merged",
                       "withWebsite", "withEmail", "withPhone"), 0)
    pending: list[dict] = []
    write_s = 0.0
    write_calls = 0

    def flush():
        nonlocal write_s, write_calls
        if not pending:
            return
        t = time.monotonic()
        results = leads_mod.upsert_overture_batch(db.leads, owner_id=owner, job_id=job, items=pending, now=ov.utcnow())
        write_s += time.monotonic() - t
        write_calls += 1
        for r in results:
            if isinstance(r, Exception):
                s["failures"] = s.get("failures", 0) + 1
            else:
                s[{"new": "newLeads", "merged": "merged"}.get(r, "updated")] += 1
        pending.clear()

    t_leads = time.monotonic()
    for p in pins:
        for _, row in ov.read_rows(cache, p):
            s["rows"] += 1
            if not area.contains(float(row["lat"]), float(row["lon"])):
                s["outsideArea"] += 1
                continue
            lead = ovm.row_to_lead(row, release=release, now=ov.utcnow())
            if lead is None:
                s["noCategory"] += 1
                continue
            s["discovered"] += 1
            s["withWebsite"] += "website" in lead
            s["withEmail"] += bool(lead.get("emails"))
            s["withPhone"] += bool(lead.get("phones"))
            pending.append(lead)
            if len(pending) >= BATCH:
                flush()
        flush()
        cache.unpin(p)
    lead_s = time.monotonic() - t_leads
    t1 = time.monotonic()
    again = cache.ensure(release, tiles)
    repeat = time.monotonic() - t1
    for p in again:
        cache.unpin(p)
    print(json.dumps({
        "place": area.display_name, "release": release, "tiles": len(tiles), "duckdbQueries": len(queries),
        "firstSearchSeconds": round(first, 1), "peakRssMB": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
        "cacheMB": round(cache.used_bytes() / 2**20, 2), "gymPlacesInTiles": sum(p.features for p in pins),
        "leads": s, "uniqueLeadsInDb": db.leads.count_documents({}),
        "repeatSearch": {"seconds": round(repeat, 3), "newQueries": len(queries) - 1},
        "leadStage": {"seconds": round(lead_s, 1), "writeSeconds": round(write_s, 1), "batches": write_calls,
                      "leadsPerSecond": round(s["discovered"] / lead_s, 1) if lead_s else None},
        "minConfidence": cfg.min_confidence,
    }, indent=2))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "gn:2643743")
