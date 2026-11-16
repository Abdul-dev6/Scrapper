"""Phase 2c: Overture Maps Places - the real DuckDB query on generated GeoParquet (Overture's schema),
categories, missing fields, dedupe within the lane, releases, checkpoints, concurrency, isolation,
and strict separation from the OSM lane."""

import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from bson import ObjectId

from app import jobs as q
from app import pipeline
from app.overture import mapping as ovm
from app.overture import places as ov
from app.pipeline import run_job
from tests.conftest import OWNER, FakeOSM, FakeWeb, add_job

duckdb = pytest.importorskip("duckdb")

REL = "2026-10-21.0"
SCHEMA = """
    id VARCHAR,
    names STRUCT("primary" VARCHAR),
    taxonomy STRUCT("primary" VARCHAR, hierarchy VARCHAR[], alternates VARCHAR[]),
    confidence DOUBLE, operating_status VARCHAR,
    websites VARCHAR[], socials VARCHAR[], emails VARCHAR[], phones VARCHAR[],
    addresses STRUCT(freeform VARCHAR, locality VARCHAR, postcode VARCHAR, region VARCHAR, country VARCHAR)[],
    sources STRUCT(property VARCHAR, dataset VARCHAR, license VARCHAR, record_id VARCHAR, update_time VARCHAR)[],
    bbox STRUCT(xmin DOUBLE, xmax DOUBLE, ymin DOUBLE, ymax DOUBLE)
"""


def place(gid, name, lat, lon, cat="gym", *, alts=None, conf=0.9, status=None, website=None, phone=None, email=None,
          social=None, addr=True, record=None, dataset="meta", license="CDLA-Permissive-2.0"):
    return {
        "id": gid, "names": {"primary": name}, "taxonomy": {"primary": cat, "hierarchy": None, "alternates": alts},
        "confidence": conf, "operating_status": status,
        "websites": [website] if website else None, "socials": [social] if social else None,
        "emails": [email] if email else None, "phones": [phone] if phone else None,
        "addresses": [{"freeform": "20 Grosvenor Place", "locality": "London", "postcode": "SW1X 7HN", "region": "ENG",
                       "country": "GB"}] if addr else None,
        "sources": [{"property": "", "dataset": dataset, "license": license, "record_id": record or f"r-{gid}",
                     "update_time": "2026-10-12T00:00:00.000Z"}],
        "bbox": {"xmin": lon, "xmax": lon, "ymin": lat, "ymax": lat},
    }


def write_release(settings, release, rows):
    """A GeoParquet release folder in the layout the S3 bucket uses: release/<r>/theme=places/type=place/."""
    folder = Path(settings.overture_source.format(release=release)).parent
    folder.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"CREATE TABLE p ({SCHEMA})")
    cols = ["id", "names", "taxonomy", "confidence", "operating_status", "websites", "socials", "emails", "phones",
            "addresses", "sources", "bbox"]
    for r in rows:
        con.execute(f"INSERT INTO p VALUES ({','.join('?' * len(cols))})", [r[c] for c in cols])
    con.execute(f"COPY p TO '{(folder / 'part-00000.parquet').as_posix()}' (FORMAT parquet)")
    con.close()


ROWS = [
    place("gers-iron", "Iron Fist Gym", 51.50, -0.10, website="https://ironfist.test", phone="+442079460001",
          email="info@ironfist.test", social="https://www.facebook.com/ironfist", record="111"),
    place("gers-box", "Southpaw Boxing", 51.51, -0.12, cat="boxing_gym", phone="+442079460002", addr=False),
    place("gers-bjj", "Roll BJJ", 51.52, -0.13, cat="brazilian_jiu_jitsu_club"),
    place("gers-alt", "Studio X", 51.53, -0.14, cat="yoga_studio", alts=["gym"]),  # matched by an alternate
    place("gers-pt", "Joe Personal Training", 51.50, -0.11, cat="fitness_trainer"),  # not a gym: excluded
    place("gers-closed", "Closed Gym", 51.50, -0.12, status="permanently_closed"),
    place("gers-junk", "Maybe Gym", 51.50, -0.13, conf=0.2),  # below the confidence threshold
    place("gers-paris", "Paris Gym", 48.85, 2.35),  # outside the area
    place("gers-noname", "", 51.50, -0.14),
]


def ov_job(db, settings, *, sources=("overture",), categories=("gym", "boxing_club", "martial_arts_school"),
           fake=None, owner=OWNER, query=None, elements=None):
    fake = fake or FakeOSM(elements=elements or [])
    add_job(db, sources=list(sources), categories=list(categories), ownerId=owner)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    status = run_job(db, job, settings, client=fake.client(settings), sleep=lambda _s: None,
                     web_transport=FakeWeb().transport(), mx_resolve=FakeWeb().resolve, overture_query=query)
    return status, db.scrapingJobs.find_one({"_id": job["_id"]}), fake


class CountingQuery:
    """The real DuckDB query, counting how often Overture is actually read."""

    def __init__(self, settings):
        self.real = ov.query_duckdb(ov.OvertureConfig.from_settings(settings))
        self.calls = []

    def __call__(self, release, bbox, beat):
        self.calls.append((release, bbox))
        yield from self.real(release, bbox, beat)


# ---------------------------------------------------------------- query, categories, fields


def test_duckdb_query_filters_area_categories_status_and_confidence(db, settings):
    write_release(settings, REL, ROWS)
    rows = list(ov.query_duckdb(ov.OvertureConfig.from_settings(settings))(REL, (51.0, -1.0, 52.0, 0.0), lambda: None))
    assert {r["id"] for r in rows} == {"gers-iron", "gers-box", "gers-bjj", "gers-alt", "gers-noname"}
    iron = next(r for r in rows if r["id"] == "gers-iron")
    assert iron["name"] == "Iron Fist Gym" and iron["lat"] == pytest.approx(51.50) and iron["cat"] == "gym"
    assert iron["sources"][0]["license"] == "CDLA-Permissive-2.0"


def test_category_mapping_uses_verified_ids_only():
    assert ov.categories_for("gym", None) == ["gym", "fitness_center"]
    assert ov.categories_for("boxing_class", None) == ["boxing_club"]
    assert ov.categories_for("yoga_studio", ["gym"]) == ["gym", "fitness_center"]
    assert ov.categories_for("fitness_trainer", None) == [] and ov.categories_for(None, None) == []
    assert "mma_academy" not in {c for cs in ov.TAXONOMY_TO_CATEGORIES.values() for c in cs}  # none verified


def test_row_mapping_keeps_only_present_fields_with_provenance(db, settings):
    write_release(settings, REL, ROWS)
    rows = {r["id"]: r for r in ov.query_duckdb(ov.OvertureConfig.from_settings(settings))(REL, (51, -1, 52, 0), lambda: None)}
    now = q.now()
    iron = ovm.row_to_lead(rows["gers-iron"], release=REL, now=now)
    assert iron["dataLane"] == "overture" and iron["externalIds"] == {"overture": "gers-iron", "overtureSources": ["meta:111"]}
    assert iron["website"]["domain"] == "ironfist.test" and iron["phones"][0]["raw"] == "+442079460001"
    assert iron["emails"][0]["value"] == "info@ironfist.test" and iron["socials"]["facebook"]["sourceType"] == "overture"
    assert iron["address"] == {"street": "20 Grosvenor Place", "city": "London", "region": "ENG", "postcode": "SW1X 7HN",
                               "countryCode": "GB"}
    assert iron["overture"]["release"] == REL and iron["overture"]["sources"][0]["license"] == "CDLA-Permissive-2.0"
    assert iron["fieldSources"]["name"] == {"sourceUrl": f"overture:{REL}:gers-iron", "sourceType": "overture"}
    bare = ovm.row_to_lead(rows["gers-box"], release=REL, now=now)
    assert "address" not in bare and "website" not in bare and "emails" not in bare  # missing stays missing
    assert ovm.row_to_lead(rows["gers-noname"], release=REL, now=now) is None


# ---------------------------------------------------------------- full jobs


def test_overture_only_job_saves_leads_in_the_overture_lane(db, settings):
    write_release(settings, REL, ROWS)
    status, job, fake = ov_job(db, settings)
    assert status == "completed"
    leads = {d["externalIds"]["overture"]: d for d in db.leads.find()}
    assert set(leads) == {"gers-iron", "gers-box", "gers-bjj", "gers-alt"}  # Paris (outside), PT, closed, junk excluded
    assert all(d["dataLane"] == "overture" for d in leads.values())
    assert leads["gers-iron"]["phones"][0]["e164"] == "+442079460001"  # normalized on save
    assert job["location"]["overture"]["release"] == REL and "CDLA" in job["location"]["overture"]["license"]
    assert job["stats"]["overture"]["newLeads"] == 4
    assert fake.downloads() == [] and not any(r.url.host == "geofabrik.test" for r in fake.calls)  # no OSM at all


def test_job_categories_filter_overture_too(db, settings):
    write_release(settings, REL, ROWS)
    ov_job(db, settings, categories=("boxing_club",))
    assert {d["externalIds"]["overture"] for d in db.leads.find()} == {"gers-box"}


def test_osm_and_overture_stay_separate_even_for_the_same_gym(db, settings):
    """The same gym in both sources (same name, spot and phone) gives TWO leads, one per lane; never merged."""
    write_release(settings, REL, [place("gers-iron", "Iron Fist", 51.5, -0.1, phone="+442079460001", record="111")])
    osm = [{"type": "node", "id": 1, "lat": 51.5, "lon": -0.1,
            "tags": {"name": "Iron Fist", "leisure": "fitness_centre", "phone": "+44 20 7946 0001"}}]
    status, job, _ = ov_job(db, settings, sources=("osm", "overture"), categories=("gym",), elements=osm)
    assert status == "completed"
    docs = list(db.leads.find())
    assert sorted(d.get("dataLane", "osm") for d in docs) == ["osm", "overture"]
    osm_lead = next(d for d in docs if d.get("dataLane") != "overture")
    ov_lead = next(d for d in docs if d.get("dataLane") == "overture")
    assert "overture" not in osm_lead["externalIds"] and "osm" not in ov_lead["externalIds"]
    assert not osm_lead.get("mergeLog") and not ov_lead.get("mergeLog")
    assert osm_lead["dedupeKey"] != ov_lead["dedupeKey"] and ov_lead["dedupeKey"].startswith("overture|")
    assert job["stats"]["newLeads"] == 1 and job["stats"]["overture"]["newLeads"] == 1


def test_duplicates_within_overture_merge_but_branches_stay_separate(db, settings):
    write_release(settings, REL, [
        place("gers-a", "PureGym Holborn", 51.5000, -0.1000, phone="+442079460009", record="1"),
        place("gers-a-dup", "PureGym Holborn", 51.5003, -0.1001, record="2", dataset="Foursquare", license="Apache-2.0"),
        place("gers-b", "PureGym Holborn", 51.5300, -0.1500, record="3"),  # same name, 3.6 km away: a branch
    ])
    ov_job(db, settings, categories=("gym",))
    docs = list(db.leads.find())
    assert len(docs) == 2
    merged = next(d for d in docs if d["externalIds"]["overture"] == "gers-a")
    assert merged["externalIds"]["overtureAliases"] == ["gers-a-dup"]
    assert set(merged["externalIds"]["overtureSources"]) == {"meta:1", "foursquare:2"}


def test_repeat_search_reuses_the_cached_release(db, settings):
    write_release(settings, REL, ROWS)
    query = CountingQuery(settings)
    ov_job(db, settings, query=query)
    _, job2, _ = ov_job(db, settings, query=query)
    assert len(query.calls) == 1  # the second search read nothing from Overture
    assert job2["stats"]["overture"]["newLeads"] == 0 and job2["stats"]["overture"]["updated"] == 4
    assert db.leads.count_documents({}) == 4


def test_new_release_is_used_and_a_changed_gers_id_updates_the_same_lead(db, settings):
    write_release(settings, REL, [place("gers-old", "Iron Fist", 51.5, -0.1, record="111")])
    fake = FakeOSM()
    ov_job(db, settings, categories=("gym",), fake=fake)
    new_rel = "2026-11-18.0"
    write_release(settings, new_rel, [place("gers-new", "Iron Fist Boxing Gym", 51.5, -0.1, record="111")])
    fake.overture_latest = new_rel
    db.overtureReleases.update_one({"_id": "latest"}, {"$set": {"checkedAt": q.now() - timedelta(hours=7)}})
    _, job, _ = ov_job(db, settings, categories=("gym",), fake=fake)
    assert job["location"]["overture"]["release"] == new_rel
    [lead] = list(db.leads.find())
    assert lead["externalIds"]["overture"] == "gers-old" and lead["externalIds"]["overtureAliases"] == ["gers-new"]
    assert lead["overture"]["release"] == new_rel


def test_release_catalogue_down_uses_last_known_release_else_retries(db, settings):
    cfg = ov.OvertureConfig.from_settings(settings)
    fake = FakeOSM()
    fake.stac_status = 503
    with pytest.raises(ov.OvertureUnavailable):
        ov.current_release(db, fake.client(settings), cfg)
    db.overtureReleases.insert_one({"_id": "latest", "release": REL, "checkedAt": q.now() - timedelta(days=2)})
    assert ov.current_release(db, fake.client(settings), cfg) == REL


def test_too_large_areas_fail_clearly():
    from shapely.geometry import box
    with pytest.raises(ov.ExtractTooLarge, match="smaller area"):
        ov.tiles_for(box(-125, 24, -66, 49), max_tiles=400)
    assert ov.tiles_for(box(-0.51, 51.28, 0.33, 51.69), max_tiles=400) == ["51_-1", "51_0"]


def test_checkpoint_resume_uses_the_same_release_without_duplicates(db, settings, monkeypatch):
    rows = [place(f"gers-{i}", f"Gym {i}", 51.40 + i * 0.001, -0.20, record=str(i)) for i in range(60)]
    write_release(settings, REL, rows)
    real_save = q.save_checkpoint
    saves = []

    class Crash(BaseException):
        pass

    def crash(jobs, job_id, key, value, **kw):
        real_save(jobs, job_id, key, value, **kw)
        if key == "overture" and value.get("position"):
            saves.append(1)
            if len(saves) == 2:
                raise Crash()

    monkeypatch.setattr(q, "save_checkpoint", crash)
    monkeypatch.setattr(pipeline, "OVERTURE_WRITE_BATCH", 25)  # checkpoints after each written batch of 25
    add_job(db, sources=["overture"], categories=["gym"])
    job = q.claim_next_job(db.scrapingJobs, "w1")
    fake = FakeOSM()
    with pytest.raises(Crash):
        run_job(db, job, settings, client=fake.client(settings), sleep=lambda _s: None, web_transport=FakeWeb().transport())
    assert db.leads.count_documents({}) == 50
    monkeypatch.setattr(q, "save_checkpoint", real_save)
    fake.overture_latest = "2026-11-18.0"  # a newer release appears: the resumed job must NOT switch
    db.scrapingJobs.update_one({"_id": job["_id"]}, {"$set": {"lockedAt": q.now() - timedelta(hours=1)}})
    q.recover_stale_jobs(db.scrapingJobs, 600, 3, rand=lambda: 0)
    db.scrapingJobs.update_one({"_id": job["_id"]}, {"$set": {"nextAttemptAt": q.now()}})
    job = q.claim_next_job(db.scrapingJobs, "w2")
    query = CountingQuery(settings)
    assert run_job(db, job, settings, client=fake.client(settings), sleep=lambda _s: None,
                   web_transport=FakeWeb().transport(), overture_query=query) == "completed"
    assert query.calls == []  # resumed from the pinned tiles, nothing re-read
    done = db.scrapingJobs.find_one({"_id": job["_id"]})
    assert db.leads.count_documents({}) == 60 and done["stats"]["overture"]["newLeads"] == 60
    assert done["checkpoint"]["overture"]["release"] == REL


def test_two_jobs_for_the_same_tiles_query_overture_once(db, settings):
    write_release(settings, REL, ROWS)
    gate = threading.Event()
    real = ov.query_duckdb(ov.OvertureConfig.from_settings(settings))
    calls = []

    def slow(release, bbox, beat):
        calls.append(1)
        gate.wait(5)
        yield from real(release, bbox, beat)

    cfg = ov.OvertureConfig.from_settings(settings)
    lock = threading.Lock()
    results = {}

    class Serialized:
        def __init__(self, coll):
            self._c = coll

        def __getattr__(self, n):
            a = getattr(self._c, n)
            if not callable(a):
                return a

            def f(*x, **k):
                with lock:
                    return a(*x, **k)
            return f

    def worker(owner, sleep):
        c = ov.TileCache(db, cfg, owner=owner, query_fn=slow, sleep=sleep)
        c.coll = Serialized(db.overtureTiles)
        results[owner] = c.ensure(REL, ["51_-1"])

    t1 = threading.Thread(target=worker, args=("a", lambda s: time.sleep(0.01)))
    t1.start()
    time.sleep(0.05)
    t2 = threading.Thread(target=worker, args=("b", lambda s: (gate.set(), time.sleep(0.01))))
    t2.start()
    t1.join(10)
    t2.join(10)
    assert len(calls) == 1 and results["a"][0].file == results["b"][0].file


def test_users_are_isolated(db, settings):
    write_release(settings, REL, ROWS)
    other = ObjectId()
    ov_job(db, settings)
    ov_job(db, settings, owner=other)
    assert db.leads.count_documents({"ownerId": OWNER}) == 4 and db.leads.count_documents({"ownerId": other}) == 4
    # the shared tile cache is public map data; leads (and notes etc.) are per user
    assert db.overtureTiles.count_documents({"status": "ready"}) == 2


def test_eviction_prefers_old_releases_and_keeps_pinned_tiles(db, settings):
    write_release(settings, REL, ROWS)
    cfg = ov.OvertureConfig.from_settings(settings)
    cache = ov.TileCache(db, cfg, owner="w")
    [old] = cache.ensure(REL, ["51_-1"])
    cache.unpin(old)
    write_release(settings, "2026-11-18.0", ROWS)
    db.overtureReleases.insert_one({"_id": "latest", "release": "2026-11-18.0", "checkedAt": q.now()})
    [new] = cache.ensure("2026-11-18.0", ["51_-1"])  # pinned (in use)
    from dataclasses import replace
    cache.cfg = replace(cfg, cache_max_bytes=cache.used_bytes())
    cache.make_room(1)
    assert not cache.abs(old.file).exists() and cache.abs(new.file).exists()
    with pytest.raises(ov.DiskSpaceError):
        cache.make_room(10**9)
    assert cache.abs(new.file).exists()
