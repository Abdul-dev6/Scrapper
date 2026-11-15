"""Phase 2b: Geofabrik extracts - index, region choice, download, cache, leases, eviction, recovery.

Local runs use tests/conftest.py's fake PBF filter; the real pyosmium filter is covered by
tests/test_extracts_osmium.py (production Docker image)."""

import os
import threading
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from shapely.geometry import box

from app import jobs as q
from app.categories import CATEGORY_OSM_FILTERS
from app.geo.tiles import Tile, initial_tiles
from app.osm import extracts as ex
from app.osm.ratelimit import RateLimiter
from app.osm.tags import is_wanted, match_categories, parse_selector, selector_keys
from tests.conftest import FakeOSM

REGION = "great-britain"


def gym(i, name="Gym", lat=51.5, lon=-0.1, **tags):
    return {"type": "node", "id": i, "lat": lat, "lon": lon, "tags": {"name": name, "leisure": "fitness_centre", **tags}}


ELEMENTS = [gym(1, "Iron Fist"), gym(2, "Southpaw"), {"type": "node", "id": 3, "lat": 51.5, "lon": -0.1,
                                                        "tags": {"amenity": "cafe", "name": "Not a gym"}}]


def make(db, settings, fake, owner="w1", **cfg_over):
    cfg = replace(ex.ExtractConfig.from_settings(settings), **cfg_over)
    client = fake.client(settings)
    cache = ex.ExtractCache(db, cfg, owner=owner, client=client, sleep=lambda _s: None)
    index = ex.GeofabrikIndex.load(client, cfg)
    return cache, index, cfg


def region(index, rid=REGION):
    return index.by_id[rid]


def files_on_volume(cfg):
    return sorted(str(p.relative_to(cfg.cache_dir)).replace("\\", "/") for p in (cfg.cache_dir / "files").rglob("*")
                  if p.is_file())


# ---------------------------------------------------------------- selectors / tiles / limiter (migrated)


def test_every_category_selector_is_understood_by_the_local_matcher():
    for selectors in CATEGORY_OSM_FILTERS.values():
        for s in selectors:
            assert parse_selector(s)


def test_match_categories_and_wanted_rule():
    assert match_categories({"leisure": "fitness_centre"}) == ["gym", "fitness_center"]
    assert "boxing_club" in match_categories({"sport": "boxing;fitness"})
    assert match_categories({"amenity": "cafe"}) == []
    assert is_wanted({"name": "X", "sport": "boxing"}) and not is_wanted({"sport": "boxing"})  # unnamed = no lead
    assert not is_wanted({"name": "  ", "sport": "boxing"})
    assert {"leisure", "sport", "amenity", "shop"} <= selector_keys()


def test_tiles():
    t = Tile(0, 0, 2, 2)
    assert [q.bbox for q in t.split()] == [[0, 0, 1, 1], [0, 1, 1, 2], [1, 0, 2, 1], [1, 1, 2, 2]]
    assert initial_tiles([-18, 177, -16, -179]) == [Tile(-18, 177, -16, 180.0), Tile(-18, -180.0, -16, -179)]


def test_rate_limiter_spaces_out_calls():
    now = [0.0]
    slept = []
    rl = RateLimiter(1.0, clock=lambda: now[0], sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    rl.wait()
    now[0] += 0.25
    rl.wait()
    assert slept == [pytest.approx(0.75)]


# ---------------------------------------------------------------- index + region choice


def test_index_is_cached_on_the_volume_and_reused(db, settings):
    fake = FakeOSM()
    make(db, settings, fake)
    make(db, settings, fake)
    assert sum(1 for r in fake.calls if r.url.path == "/index-v1.json") == 1


def test_index_refresh_failure_keeps_using_the_cached_copy(db, settings):
    cache, index, cfg = make(db, settings, FakeOSM())
    path = cfg.cache_dir / "index" / "index-v1.json"
    old = time.time() - 8 * 86400
    os.utime(path, (old, old))  # a week old: due for a refresh, but Geofabrik's index is down
    again = ex.GeofabrikIndex.load(FakeOSM(index_status=503).client(settings), cfg)
    assert REGION in again.by_id


def test_no_index_at_all_is_a_temporary_error(db, settings):
    cfg = ex.ExtractConfig.from_settings(settings)
    with pytest.raises(ex.ExtractUnavailable):
        ex.GeofabrikIndex.load(FakeOSM(index_status=503).client(settings), cfg)


def _regions_fake():
    return FakeOSM(regions={
        "england": ([49.9, -6.0, 55.9, 1.8], "england-261113"),
        "greater-london": ([51.28, -0.52, 51.70, 0.34], "greater-london-261113"),
        "kent": ([50.9, 0.0, 51.8, 1.5], "kent-261113"),
    })


def test_plan_uses_actual_download_sizes(db, settings):
    fake = _regions_fake()
    _, index, _ = make(db, settings, fake)
    london = box(-0.51, 51.28, 0.33, 51.69)
    sizes = {"england": 1500, "greater-london": 100, "kent": 60}
    plan = index.plan(london, size_of=lambda r: sizes[r.id], max_download_bytes=10**9, max_job_download_bytes=10**9)
    assert [r.id for r in plan] == ["greater-london"]  # not all of England
    wider = box(-0.51, 51.28, 1.2, 51.69)  # London + part of Kent
    plan = index.plan(wider, size_of=lambda r: sizes[r.id], max_download_bytes=10**9, max_job_download_bytes=10**9)
    assert {r.id for r in plan} == {"greater-london", "kent"}
    sizes["kent"] = 5000  # now one England file is cheaper than London + a huge Kent
    plan = index.plan(wider, size_of=lambda r: sizes[r.id], max_download_bytes=10**9, max_job_download_bytes=10**9)
    assert [r.id for r in plan] == ["england"]


def test_plan_prefers_already_cached_extracts_and_ignores_border_slivers(db, settings):
    _, index, _ = make(db, settings, _regions_fake())
    london_plus_sliver = box(-0.51, 51.28, 0.3401, 51.69)  # 0.0001° outside Greater London's outline
    sizes = {"england": 1500, "greater-london": 100, "kent": 60}
    plan = index.plan(london_plus_sliver, size_of=lambda r: sizes[r.id], max_download_bytes=10**9,
                      max_job_download_bytes=10**9)
    assert [r.id for r in plan] == ["greater-london"]  # no extra download for a hairline sliver
    plan = index.plan(box(-0.51, 51.28, 1.2, 51.69), cached=["england"], size_of=lambda r: sizes[r.id],
                      max_download_bytes=10**9, max_job_download_bytes=10**9)
    assert [r.id for r in plan] == ["england"]  # cached: costs nothing to reuse


def test_too_large_areas_fail_with_an_actionable_message(db, settings):
    _, index, _ = make(db, settings, _regions_fake())
    with pytest.raises(ex.ExtractTooLarge, match="Search a smaller area"):
        index.plan(box(-5, 50, 1.7, 55), size_of=lambda r: 9 * ex.GB, max_download_bytes=6 * ex.GB,
                   max_job_download_bytes=8 * ex.GB)
    with pytest.raises(ex.ExtractError, match="No OpenStreetMap extract"):
        index.plan(box(20, 10, 21, 11), size_of=lambda r: 1, max_download_bytes=10, max_job_download_bytes=10)


# ---------------------------------------------------------------- cache freshness


def test_missing_cache_is_downloaded_and_prepared(db, settings):
    fake = FakeOSM(elements=ELEMENTS)
    cache, index, cfg = make(db, settings, fake)
    stages = []
    [pin] = cache.ensure([region(index)], on_stage=stages.append)
    rows = [el for _, el in ex.read_elements(cache.abs(pin.file))]
    assert [r["id"] for r in rows] == [1, 2]  # only named gyms; the cafe is filtered out
    assert pin.version == "great-britain-261113" and pin.features == 2 and len(pin.sha256) == 64
    assert any("Downloading" in s for s in stages) and any("Preparing gym data" in s for s in stages)
    doc = db.osmExtracts.find_one({"_id": REGION})
    assert doc["status"] == "ready" and doc["current"]["sourceMd5"] and doc["current"]["osmTimestamp"].startswith("2026-11-13")
    assert doc["refreshLease"] is None
    assert list(cfg.tmp_dir.glob("*")) == []  # the original download is deleted after filtering
    assert len(fake.downloads()) == 1


def test_fresh_cache_is_used_without_any_download(db, settings):
    fake = FakeOSM(elements=ELEMENTS)
    cache, index, _ = make(db, settings, fake)
    for p in cache.ensure([region(index)]):
        cache.unpin(p)
    stages = []
    [pin] = cache.ensure([region(index)], on_stage=stages.append)
    assert len(fake.downloads()) == 1 and stages == ["Using cached OpenStreetMap data for Great Britain"]
    assert db.osmExtracts.find_one({"_id": REGION}).get("refreshRequestedAt") is None


def test_stale_cache_is_used_now_and_refreshed_in_the_background(db, settings):
    fake = FakeOSM(elements=ELEMENTS)
    cache, index, _ = make(db, settings, fake)
    [old] = cache.ensure([region(index)])
    cache.unpin(old)
    db.osmExtracts.update_one({"_id": REGION}, {"$set": {"current.refreshedAt": q.now() - timedelta(days=8)}})
    fake.elements = [*ELEMENTS, gym(4, "New Gym")]
    fake.set_version(REGION, "great-britain-261120")

    [pin] = cache.ensure([region(index)])
    assert pin.version == "great-britain-261113" and len(fake.downloads()) == 1  # no waiting for the download
    assert db.osmExtracts.find_one({"_id": REGION})["refreshRequestedAt"] is not None

    assert cache.refresh_requested(index) == 1  # the background thread's work
    doc = db.osmExtracts.find_one({"_id": REGION})
    assert doc["current"]["version"] == "great-britain-261120" and doc["current"]["features"] == 3
    assert doc["previous"]["version"] == "great-britain-261113" and doc["refreshRequestedAt"] is None
    assert cache.abs(pin.file).exists()  # the running job's pinned file is still there
    assert sum(1 for _ in ex.read_elements(cache.abs(pin.file))) == 2  # ...and unchanged (immutable)


def test_unchanged_source_version_only_marks_the_cache_fresh(db, settings):
    fake = FakeOSM(elements=ELEMENTS)
    cache, index, _ = make(db, settings, fake)
    cache.unpin(cache.ensure([region(index)])[0])
    db.osmExtracts.update_one({"_id": REGION}, {"$set": {"current.refreshedAt": q.now() - timedelta(days=8),
                                                         "refreshRequestedAt": q.now()}})
    cache.refresh_requested(index)
    assert len(fake.downloads()) == 1  # Geofabrik has nothing newer: no second download
    assert q.now() - db.osmExtracts.find_one({"_id": REGION})["current"]["refreshedAt"] < timedelta(minutes=1)


def test_failed_refresh_keeps_the_old_cache_usable(db, settings):
    fake = FakeOSM(elements=ELEMENTS)
    cache, index, _ = make(db, settings, fake)
    [old] = cache.ensure([region(index)])
    cache.unpin(old)
    fake.set_version(REGION, "great-britain-261120")
    fake.status = 500  # Geofabrik breaks during the refresh
    db.osmExtracts.update_one({"_id": REGION}, {"$set": {"refreshRequestedAt": q.now()}})
    assert cache.refresh_requested(index) == 0
    doc = db.osmExtracts.find_one({"_id": REGION})
    assert doc["current"]["version"] == "great-britain-261113" and doc["status"] == "ready"
    assert "HTTP 500" in doc["lastError"]["message"] and doc["refreshLease"] is None
    fake.status = None
    [pin] = cache.ensure([region(index)])
    assert pin.version == "great-britain-261113" and cache.abs(pin.file).exists()


def test_invalid_pbf_is_rejected_and_nothing_is_published(db, settings):
    fake = FakeOSM(bodies={REGION: b"this is not a PBF"})
    cache, index, cfg = make(db, settings, fake)
    with pytest.raises(ex.ExtractCorrupt):
        cache.ensure([region(index)])
    doc = db.osmExtracts.find_one({"_id": REGION})
    assert doc.get("current") is None and doc["lastError"]["type"] == "ExtractCorrupt"
    assert files_on_volume(cfg) == [] and list(cfg.tmp_dir.glob("*.osm.pbf")) == []


def test_without_a_published_checksum_length_is_still_verified(db, settings):
    cache, index, _ = make(db, settings, FakeOSM(elements=ELEMENTS, md5=False))
    [pin] = cache.ensure([region(index)])
    assert db.osmExtracts.find_one({"_id": REGION})["current"]["sourceMd5"] is None and pin.features == 2


def test_wrong_checksum_is_rejected(db, settings):
    cache, index, cfg = make(db, settings, FakeOSM(elements=ELEMENTS, bad_md5=True))
    with pytest.raises(ex.ExtractCorrupt, match="checksum"):
        cache.ensure([region(index)])
    assert list(cfg.tmp_dir.glob("*")) == [] and files_on_volume(cfg) == []


# ---------------------------------------------------------------- download failures


@pytest.mark.parametrize("status,error", [(500, ex.ExtractUnavailable), (503, ex.ExtractUnavailable), (404, ex.ExtractError)])
def test_http_errors(db, settings, status, error):
    cache, index, _ = make(db, settings, FakeOSM(elements=ELEMENTS, status=status))
    with pytest.raises(error) as exc:
        cache.ensure([region(index)])
    assert isinstance(exc.value, ex.ExtractUnavailable) == (status >= 500)  # 5xx retried later, 404 not


def test_interrupted_download_resumes_safely_with_range(db, settings):
    fake = FakeOSM(elements=[gym(i) for i in range(1, 200)], cut_after=500)
    cache, index, cfg = make(db, settings, fake)
    with pytest.raises(ex.ExtractUnavailable, match="interrupted"):
        cache.ensure([region(index)])
    parts = list(cfg.tmp_dir.glob("*.part"))
    assert parts and parts[0].stat().st_size == 500
    assert db.osmExtracts.find_one({"_id": REGION}).get("current") is None  # nothing half-published
    # The worker restarts (new process, new owner): the partial file is resumed, not restarted.
    cache2, index2, _ = make(db, settings, fake, owner="w2")
    [pin] = cache2.ensure([region(index2)])
    second = fake.downloads()[-1]
    assert second.headers["range"] == "bytes=500-" and second.headers["if-range"] == '"great-britain-261113"'
    assert pin.features == 199  # the md5 of the joined file matched Geofabrik's


def test_without_range_support_an_interrupted_download_starts_again(db, settings):
    fake = FakeOSM(elements=ELEMENTS, ranges=False, cut_after=50)
    cache, index, _ = make(db, settings, fake)
    with pytest.raises(ex.ExtractUnavailable):
        cache.ensure([region(index)])
    [pin] = cache.ensure([region(index)])
    assert "range" not in fake.downloads()[-1].headers and pin.features == 2


def test_wrong_content_length_is_rejected(db, settings):
    cache, index, cfg = make(db, settings, FakeOSM(elements=ELEMENTS, wrong_length=True))
    with pytest.raises(ex.ExtractCorrupt, match="Incomplete"):
        cache.ensure([region(index)])
    assert list(cfg.tmp_dir.glob("*")) == []


def test_insufficient_temporary_disk_fails_before_downloading(db, settings, monkeypatch):
    cache, index, cfg = make(db, settings, FakeOSM(elements=ELEMENTS), tmp_margin_bytes=10**6)
    real = ex.disk_free
    monkeypatch.setattr(ex, "disk_free", lambda p: 1000 if Path(p) == cfg.tmp_dir else real(p))
    with pytest.raises(ex.DiskSpaceError, match="temporary disk"):
        cache.ensure([region(index)])
    assert list(cfg.tmp_dir.glob("*.part")) == []


def test_extract_over_the_size_limit_is_refused_before_download(db, settings):
    fake = FakeOSM(elements=ELEMENTS)
    cache, index, _ = make(db, settings, fake, max_download_bytes=10)
    with pytest.raises(ex.ExtractTooLarge, match="Search a smaller area"):
        cache.ensure([region(index)])
    assert fake.downloads() == []


# ---------------------------------------------------------------- storage + eviction


def _two_regions():
    return FakeOSM(elements=ELEMENTS, regions={"great-britain": ([49.9, -8.0, 60.9, 1.8], "great-britain-261113"),
                                               "ireland": ([51.3, -10.7, 55.5, -5.4], "ireland-261113")})


def test_insufficient_volume_space_fails_clearly_and_keeps_the_old_cache(db, settings, monkeypatch):
    fake = FakeOSM(elements=ELEMENTS)
    cache, index, cfg = make(db, settings, fake)
    [old] = cache.ensure([region(index)])
    cache.unpin(old)
    fake.set_version(REGION, "great-britain-261120")
    real = ex.disk_free
    monkeypatch.setattr(ex, "disk_free", lambda p: 10 if cfg.cache_dir in Path(p).parents or Path(p) == cfg.cache_dir
                        else real(p))
    assert cache.acquire_refresh(REGION)
    with pytest.raises(ex.DiskSpaceError, match="cache volume"):
        cache.refresh(region(index))
    cache.release_refresh(REGION)
    doc = db.osmExtracts.find_one({"_id": REGION})
    assert doc["current"]["version"] == "great-britain-261113" and cache.abs(doc["current"]["file"]).exists()


def test_cache_budget_evicts_the_least_recently_used_region(db, settings):
    fake = _two_regions()
    cache, index, cfg = make(db, settings, fake)
    for p in cache.ensure([region(index, "ireland")]):
        cache.unpin(p)
    ireland_file = db.osmExtracts.find_one({"_id": "ireland"})["current"]["file"]
    # The budget can't hold Ireland's file AND room for a new one: Ireland (least recently used) goes.
    cache.cfg = replace(cfg, filtered_estimate_bytes=4096, cache_max_bytes=cache.used_bytes() + 4095)
    [pin] = cache.ensure([region(index)])
    assert not cache.abs(ireland_file).exists()
    ie = db.osmExtracts.find_one({"_id": "ireland"})
    assert ie["current"] is None and ie["status"] == "evicted"  # metadata never points at a deleted file
    assert cache.abs(pin.file).exists()


def test_files_in_use_or_being_refreshed_are_never_evicted(db, settings):
    fake = _two_regions()
    cache, index, cfg = make(db, settings, fake)
    [ie_pin] = cache.ensure([region(index, "ireland")])  # a running job is reading Ireland
    cache.cfg = replace(cfg, filtered_estimate_bytes=4096, cache_max_bytes=cache.used_bytes() + 4095)
    with pytest.raises(ex.DiskSpaceError):
        cache.ensure([region(index)])
    assert cache.abs(ie_pin.file).exists()
    cache.unpin(ie_pin)
    db.osmExtracts.update_one({"_id": "ireland"}, {"$set": {"refreshLease": {"owner": "w9", "until": q.now() + timedelta(hours=1)}}})
    with pytest.raises(ex.DiskSpaceError):  # Ireland is being refreshed: its last valid file stays
        cache.ensure([region(index)])
    assert cache.abs(ie_pin.file).exists()


def test_old_versions_are_evicted_before_any_current_file(db, settings):
    fake = FakeOSM(elements=ELEMENTS)
    cache, index, cfg = make(db, settings, fake)
    cache.unpin(cache.ensure([region(index)])[0])
    old_file = db.osmExtracts.find_one({"_id": REGION})["current"]["file"]
    fake.set_version(REGION, "great-britain-261120")
    db.osmExtracts.update_one({"_id": REGION}, {"$set": {"refreshRequestedAt": q.now()}})
    cache.refresh_requested(index)
    # Budget = exactly what's used now: making room for 1 more byte must drop the OLD version only
    # (independent of the real disk's free space).
    cache.cfg = replace(cfg, cache_max_bytes=cache.used_bytes())
    cache.make_room(1)
    doc = db.osmExtracts.find_one({"_id": REGION})
    assert not cache.abs(old_file).exists() and doc["previous"] is None
    assert cache.abs(doc["current"]["file"]).exists()


def test_atomic_replacement_readers_see_complete_old_or_complete_new(db, settings):
    fake = FakeOSM(elements=ELEMENTS)
    cache, index, cfg = make(db, settings, fake)
    [reader] = cache.ensure([region(index)])
    fake.elements = [*ELEMENTS, gym(5, "Fifth")]
    fake.set_version(REGION, "great-britain-261120")
    assert cache.acquire_refresh(REGION) and cache.refresh(region(index))
    cache.release_refresh(REGION)
    [new] = cache.ensure([region(index)])
    assert reader.file != new.file
    assert [e["id"] for _, e in ex.read_elements(cache.abs(reader.file))] == [1, 2]
    assert [e["id"] for _, e in ex.read_elements(cache.abs(new.file))] == [1, 2, 5]
    assert not any(f.endswith(".part") for f in files_on_volume(cfg))


def test_crash_between_file_rename_and_metadata_update_is_recovered(db, settings, monkeypatch):
    fake = FakeOSM(elements=ELEMENTS)
    cache, index, cfg = make(db, settings, fake)
    cache.unpin(cache.ensure([region(index)])[0])
    good = db.osmExtracts.find_one({"_id": REGION})["current"]
    fake.set_version(REGION, "great-britain-261120")
    real_update = cache.coll.update_one

    def crash_on_publish(flt, upd, *a, **kw):
        if "current" in (upd.get("$set") or {}) and upd["$set"].get("status") == "ready":
            raise SystemExit("worker killed")  # after os.replace, before MongoDB moved the pointer
        return real_update(flt, upd, *a, **kw)

    monkeypatch.setattr(cache.coll, "update_one", crash_on_publish)
    assert cache.acquire_refresh(REGION)
    with pytest.raises(SystemExit):
        cache.refresh(region(index))
    monkeypatch.undo()
    assert db.osmExtracts.find_one({"_id": REGION})["current"]["file"] == good["file"]  # still the valid file
    orphans = [f for f in files_on_volume(cfg) if f != good["file"] and not f.startswith("index/")]
    assert len(orphans) == 1
    old = time.time() - 2 * 3600
    os.utime(cache.abs(orphans[0]), (old, old))
    restarted = ex.ExtractCache(db, cfg, owner="w2", client=fake.client(settings))
    stats = restarted.recover()
    assert stats["orphansRemoved"] == 1 and not cache.abs(orphans[0]).exists()
    assert cache.abs(good["file"]).exists()


def test_pointer_to_a_missing_file_is_never_served(db, settings):
    fake = FakeOSM(elements=ELEMENTS)
    cache, index, cfg = make(db, settings, fake)
    [pin] = cache.ensure([region(index)])
    cache.unpin(pin)
    cache.abs(pin.file).unlink()  # e.g. the volume was replaced
    assert cache.recover()["pointersCleared"] == 1
    assert db.osmExtracts.find_one({"_id": REGION})["status"] == "missing"
    [again] = cache.ensure([region(index)])
    assert cache.abs(again.file).exists() and len(fake.downloads()) == 2


# ---------------------------------------------------------------- leases + concurrency


def test_only_one_owner_gets_the_refresh_lease_and_only_the_owner_releases_it(db, settings):
    fake = FakeOSM(elements=ELEMENTS)
    a, index, _ = make(db, settings, fake, owner="worker-a")
    b, _, _ = make(db, settings, fake, owner="worker-b")
    a._ensure_doc(region(index))
    assert a.acquire_refresh(REGION) and not b.acquire_refresh(REGION)
    b.release_refresh(REGION)  # not b's lease: must not release it
    assert db.osmExtracts.find_one({"_id": REGION})["refreshLease"]["owner"] == "worker-a"
    with pytest.raises(ex.LeaseLost):
        b.renew_refresh(REGION)


def test_expired_lease_of_a_crashed_worker_can_be_taken_over(db, settings):
    fake = FakeOSM(elements=ELEMENTS)
    cache, index, _ = make(db, settings, fake, owner="survivor")
    cache._ensure_doc(region(index))
    db.osmExtracts.update_one({"_id": REGION}, {"$set": {"refreshLease": {"owner": "crashed", "until": q.now() - timedelta(seconds=1)}}})
    assert cache.acquire_refresh(REGION)
    assert db.osmExtracts.find_one({"_id": REGION})["refreshLease"]["owner"] == "survivor"


def test_reader_lease_renewal_protects_long_jobs_and_expired_leases_dont(db, settings):
    fake = _two_regions()
    cache, index, cfg = make(db, settings, fake, reader_lease_seconds=60)
    [pin] = cache.ensure([region(index, "ireland")])
    db.osmExtracts.update_one({"_id": "ireland", "readers.lease": pin.lease},
                              {"$set": {"readers.$.until": q.now() - timedelta(seconds=1)}})  # job crashed
    assert pin.file not in cache._protected_files(None)
    cache.renew([pin])  # a live job renews
    assert pin.file in cache._protected_files(None)


class _Serialized:
    """Apply each MongoDB operation whole, one at a time (mongomock has no locking; MongoDB is atomic
    per document). Lets two threads race realistically."""

    def __init__(self, coll, lock):
        self._coll, self._lock = coll, lock

    def __getattr__(self, name):
        attr = getattr(self._coll, name)
        if not callable(attr):
            return attr

        def locked(*a, **kw):
            with self._lock:
                return attr(*a, **kw)
        return locked


def test_two_jobs_for_the_same_region_download_it_exactly_once(db, settings):
    fake = FakeOSM(elements=ELEMENTS)
    release = threading.Event()
    real_handler = fake.handler

    def slow_handler(request):  # the first download takes a while
        if request.method == "GET" and request.url.path.endswith("261113.osm.pbf"):
            release.wait(5)
        return real_handler(request)

    fake.handler = slow_handler
    lock = threading.Lock()
    results, errors = {}, []

    def job(owner, wait_sleep):
        try:
            cache, index, _ = make(db, settings, fake, owner=owner)
            cache.coll = _Serialized(db.osmExtracts, lock)
            cache.sleep = wait_sleep
            [results[owner]] = cache.ensure([region(index)])
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def second_waits(_s):
        release.set()  # the second job is waiting: let the first one finish downloading
        time.sleep(0.01)

    make(db, settings, fake)  # cache the index first so both threads see it
    t1 = threading.Thread(target=job, args=("job-1", lambda s: time.sleep(0.01)))
    t1.start()
    time.sleep(0.05)
    t2 = threading.Thread(target=job, args=("job-2", second_waits))
    t2.start()
    t1.join(10)
    t2.join(10)
    assert not errors
    assert len(fake.downloads()) == 1  # exactly one download for both jobs
    assert results["job-1"].file == results["job-2"].file and results["job-1"].sha256 == results["job-2"].sha256
    readers = db.osmExtracts.find_one({"_id": REGION})["readers"]
    assert {r["owner"] for r in readers} == {"job-1", "job-2"}  # both pinned the same immutable file


def test_no_overpass_anywhere_in_the_worker():
    root = Path(__file__).resolve().parents[1] / "app"
    hits = [str(p) for p in root.rglob("*.py") if "overpass-api" in p.read_text(encoding="utf-8")
            or "/api/interpreter" in p.read_text(encoding="utf-8")]
    assert hits == []


# ---------------------------------------------------------------- background refresher (thread + processes)


def _stale(db, cache, index, fake):
    cache.unpin(cache.ensure([region(index)])[0])
    db.osmExtracts.update_one({"_id": REGION}, {"$set": {"refreshRequestedAt": q.now()}})
    fake.set_version(REGION, "great-britain-261120")


def test_background_refresher_starts_recovers_refreshes_and_stops_cleanly(db, settings):
    fake = FakeOSM(elements=ELEMENTS)
    cache, index, cfg = make(db, settings, fake)
    _stale(db, cache, index, fake)
    r = ex.BackgroundRefresher(db, cfg, owner="w1:refresher", interval=3600, transport=__import__("httpx").MockTransport(fake.handler))
    assert set(r.start()) == {"partsRemoved", "orphansRemoved", "pointersCleared", "tmpRemoved"}
    assert r.run_once() == 1
    assert db.osmExtracts.find_one({"_id": REGION})["current"]["version"] == "great-britain-261120"
    r.stop(timeout=5)
    assert not r.thread.is_alive()


def test_shutdown_during_a_background_refresh_keeps_the_old_file(db, settings):
    import httpx

    fake = FakeOSM(elements=ELEMENTS)
    cache, index, cfg = make(db, settings, fake)
    _stale(db, cache, index, fake)
    r = ex.BackgroundRefresher(db, cfg, owner="w1:refresher", transport=httpx.MockTransport(fake.handler))
    r.stop_event.set()  # SIGTERM arrives while the refresh would run
    with pytest.raises(ex.RefreshStopped):
        r.cache.refresh_requested(ex.GeofabrikIndex.load(r.client, cfg), heartbeat=r._heartbeat)
    doc = db.osmExtracts.find_one({"_id": REGION})
    assert doc["current"]["version"] == "great-britain-261113" and doc["refreshLease"] is None


def test_two_worker_processes_never_refresh_the_same_region_twice(db, settings):
    fake = FakeOSM(elements=ELEMENTS)
    a, index, _ = make(db, settings, fake, owner="process-a")
    b, _, _ = make(db, settings, fake, owner="process-b")
    _stale(db, a, index, fake)
    downloads_before = len(fake.downloads())
    assert a.acquire_refresh(REGION)  # process A is refreshing (lease taken)
    assert b.refresh_requested(index) == 0  # process B's refresher skips it
    assert len(fake.downloads()) == downloads_before
    a.refresh(region(index))
    a.release_refresh(REGION)
    assert len(fake.downloads()) == downloads_before + 1


def test_eviction_never_deletes_a_file_pinned_in_the_same_instant(db, settings, monkeypatch):
    """Race: a job pins Ireland's file right after eviction decided it was unprotected."""
    fake = _two_regions()
    cache, index, cfg = make(db, settings, fake)
    cache.unpin(cache.ensure([region(index, "ireland")])[0])
    ie_file = db.osmExtracts.find_one({"_id": "ireland"})["current"]["file"]
    real_protected = cache._protected_files

    def protected_then_someone_pins(protect_region):
        files = real_protected(protect_region)  # computed BEFORE the pin below
        db.osmExtracts.update_one({"_id": "ireland"}, {"$push": {"readers": {
            "lease": "late", "owner": "job-x", "file": ie_file, "until": q.now() + timedelta(minutes=10)}}})
        return files

    monkeypatch.setattr(cache, "_protected_files", protected_then_someone_pins)
    cache.cfg = replace(cfg, filtered_estimate_bytes=4096, cache_max_bytes=cache.used_bytes() + 4095)
    with pytest.raises(ex.DiskSpaceError):
        cache.ensure([region(index)])
    assert cache.abs(ie_file).exists()  # the atomic "no live reader" condition kept it
    assert db.osmExtracts.find_one({"_id": "ireland"})["current"]["file"] == ie_file


def test_startup_fails_clearly_if_the_cache_volume_is_not_writable(db, settings, monkeypatch):
    cfg = ex.ExtractConfig.from_settings(settings)
    real_write = Path.write_bytes

    def denied(self, data):
        if ".write-test-" in self.name and Path(cfg.cache_dir) in self.parents:
            raise PermissionError(13, "Permission denied")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", denied)
    r = ex.BackgroundRefresher(db, cfg, owner="w1:refresher")
    with pytest.raises(RuntimeError, match="OSM_CACHE_DIR=.* is not writable.*Volume at /data"):
        r.start()
    assert not r.thread.is_alive()
