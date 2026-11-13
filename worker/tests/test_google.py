"""Google Places (New) integration: Text Search, Place Details, usage limits, merge with OSM, compliance."""

import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import jobs as q
from app.google import discovery, merge
from app.google.places import DETAILS_FIELDS, TEXT_SEARCH_FIELDS, PlacesClient, QuotaExhausted
from app.google.runner import make_usage
from app.google.usage import DETAILS, TEXT_SEARCH, UsageLimiter, month_key
from app.pipeline import run_job
from tests.conftest import OWNER, FakeOSM, FakeWeb, add_job

KEY = "test-key-NEVER-LOG-ME"
BASE = "https://places.test/v1"
LONDON_BBOX = [51.28, -0.51, 51.69, 0.33]


def gplace(pid, name, lat, lon, postcode="", number="", route="", status="OPERATIONAL"):
    comps = []
    if number:
        comps.append({"longText": number, "shortText": number, "types": ["street_number"]})
    if route:
        comps.append({"longText": route, "shortText": route, "types": ["route"]})
    if postcode:
        comps.append({"longText": postcode, "shortText": postcode, "types": ["postal_code"]})
    comps.append({"longText": "United Kingdom", "shortText": "GB", "types": ["country"]})
    return {"id": pid, "displayName": {"text": name, "languageCode": "en"},
            "location": {"latitude": lat, "longitude": lon},
            "formattedAddress": f"{number} {route}, London {postcode}, UK", "addressComponents": comps,
            "types": ["gym"], "businessStatus": status}


class FakeGoogle:
    """Plays places.googleapis.com. Returns the places inside the requested rectangle, 20 per page,
    max 60 per query (like the real API)."""

    def __init__(self, places=(), status=None):
        self.places = list(places)
        self.status = status
        self.hidden: set[str] = set()  # place ids Text Search doesn't return (Place Details still knows them)
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.status:
            return httpx.Response(self.status, json={"error": {"status": "X", "message": "nope"}})
        if request.url.path.endswith("places:searchText"):
            body = json.loads(request.content)
            r = body["locationRestriction"]["rectangle"]
            inside = [p for p in self.places if p["id"] not in self.hidden
                      and r["low"]["latitude"] <= p["location"]["latitude"] < r["high"]["latitude"]
                      and r["low"]["longitude"] <= p["location"]["longitude"] < r["high"]["longitude"]][:60]
            start = int(body.get("pageToken") or 0)
            page = inside[start:start + 20]
            out = {"places": page}
            if start + 20 < len(inside):
                out["nextPageToken"] = str(start + 20)
            return httpx.Response(200, json=out)
        pid = request.url.path.rsplit("/", 1)[1]
        for p in self.places:
            if p["id"] == pid:
                mask = request.headers["x-goog-fieldmask"].split(",")
                return httpx.Response(200, json={k: v for k, v in p.items() if k in mask})
        return httpx.Response(404, json={"error": {"status": "NOT_FOUND"}})

    def transport(self):
        return httpx.MockTransport(self.handler)

    def text_calls(self):
        return [c for c in self.calls if c.url.path.endswith("places:searchText")]

    def detail_calls(self):
        return [c for c in self.calls if not c.url.path.endswith("places:searchText")]


@pytest.fixture
def gsettings(settings):
    return replace(settings, google_api_key=KEY, google_places_url=BASE)


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


def usage_for(db, gsettings, at=None, **limits):
    s = replace(gsettings, **limits) if limits else gsettings
    return make_usage(db, s, clock=Clock(at or datetime(2026, 11, 13, 12, tzinfo=timezone.utc)))


def places_client(db, gsettings, fake, usage=None):
    return PlacesClient(httpx.Client(transport=fake.transport()), api_key=KEY, base_url=BASE,
                        usage=usage or usage_for(db, gsettings), sleep=lambda _s: None)


# ---------------------------------------------------------------- Text Search + Place Details


def test_text_search_sends_pro_field_mask_and_key_only_in_header(db, gsettings):
    fake = FakeGoogle([gplace(f"ChIJplace{i:04d}", f"Gym {i}", 51.5, -0.1) for i in range(45)])
    client = places_client(db, gsettings, fake)
    first, token = client.text_search("gym", LONDON_BBOX)
    second, token2 = client.text_search("gym", LONDON_BBOX, token)
    third, token3 = client.text_search("gym", LONDON_BBOX, token2)

    assert (len(first), len(second), len(third), token3) == (20, 20, 5, None)
    req = fake.text_calls()[0]
    assert req.method == "POST" and req.url.path == "/v1/places:searchText"
    assert req.headers["x-goog-api-key"] == KEY
    assert KEY not in str(req.url)  # never in the URL (URLs end up in logs)
    mask = req.headers["x-goog-fieldmask"].split(",")
    assert mask == TEXT_SEARCH_FIELDS.split(",")
    # Pro fields only: no Enterprise fields that would be billed at the expensive SKU
    for enterprise in ("websiteUri", "nationalPhoneNumber", "internationalPhoneNumber", "rating", "regularOpeningHours"):
        assert not any(enterprise in f for f in mask)
    body = json.loads(req.content)
    assert body["locationRestriction"]["rectangle"]["low"] == {"latitude": 51.28, "longitude": -0.51}
    assert body["pageSize"] == 20
    assert db.apiUsage.find_one()[TEXT_SEARCH] == 3  # every page is one billed request
    assert first[0]["name"] == "Gym 0" and first[0]["countryCode"] == "GB"


def test_place_details_uses_essentials_mask_only(db, gsettings):
    fake = FakeGoogle([gplace("ChIJdetails01", "Iron Fist", 51.5, -0.1, "N1 1AA", "12", "High St")])
    details = places_client(db, gsettings, fake).place_details("ChIJdetails01")

    req = fake.detail_calls()[0]
    assert req.method == "GET" and req.url.path == "/v1/places/ChIJdetails01"
    mask = req.headers["x-goog-fieldmask"]
    assert mask == DETAILS_FIELDS
    assert "displayName" not in mask  # displayName would make it a Pro request
    assert details["postcode"] == "N1 1AA" and details["houseNumber"] == "12" and details["name"] == ""
    assert db.apiUsage.find_one()[DETAILS] == 1


def test_place_details_rejects_strange_ids_without_calling(db, gsettings):
    fake = FakeGoogle()
    assert places_client(db, gsettings, fake).place_details("../../admin") is None
    assert fake.calls == []


def test_bad_key_is_fatal_and_error_never_contains_key(db, gsettings):
    fake = FakeGoogle(status=403)
    client = places_client(db, gsettings, fake)
    with pytest.raises(Exception) as exc:
        client.text_search("gym", LONDON_BBOX)
    assert "403" in str(exc.value) and KEY not in str(exc.value)
    log = db.apiUsageLog.find_one()
    assert log["ok"] is False and log["httpStatus"] == 403 and KEY not in json.dumps(log, default=str)


# ---------------------------------------------------------------- usage limits


def test_reservation_stops_at_internal_limit(db, gsettings):
    usage = usage_for(db, gsettings, google_text_search_monthly_limit=3)
    assert [usage.reserve(TEXT_SEARCH) is not None for _ in range(5)] == [True, True, True, False, False]
    doc = db.apiUsage.find_one()
    assert doc[TEXT_SEARCH] == 3 and doc["exhaustedAt"] is not None


def test_either_limit_reached_stops_all_google_calls(db, gsettings):
    usage = usage_for(db, gsettings, google_details_monthly_limit=2)
    assert usage.reserve(DETAILS) and usage.reserve(DETAILS)
    assert usage.reserve(TEXT_SEARCH) is None  # Text Search has room, but Details is full: everything stops
    assert usage.available() is False


def test_monthly_reset_resumes_google_automatically(db, gsettings):
    nov = datetime(2026, 11, 30, 20, tzinfo=timezone.utc)
    usage = usage_for(db, gsettings, at=nov, google_text_search_monthly_limit=1)
    assert usage.reserve(TEXT_SEARCH) and usage.reserve(TEXT_SEARCH) is None
    # 2026-12-01 05:00 UTC is still November in Pacific time: still blocked
    usage.clock = Clock(datetime(2026, 12, 1, 5, tzinfo=timezone.utc))
    assert usage.reserve(TEXT_SEARCH) is None
    # 2026-12-01 09:00 UTC = 01:00 Pacific: new month, new counters
    usage.clock = Clock(datetime(2026, 12, 1, 9, tzinfo=timezone.utc))
    assert usage.available() and usage.reserve(TEXT_SEARCH).month == "2026-12"
    assert {d["_id"] for d in db.apiUsage.find()} == {"google:2026-11", "google:2026-12"}


def test_month_key_uses_pacific_time():
    assert month_key(datetime(2026, 11, 1, 6, tzinfo=timezone.utc), "America/Los_Angeles") == "2026-10"
    assert month_key(datetime(2026, 11, 1, 8, tzinfo=timezone.utc), "America/Los_Angeles") == "2026-11"


def test_config_never_allows_limits_above_free_caps(monkeypatch):
    from app.config import load_settings
    monkeypatch.setenv("MONGODB_URI", "mongodb://x")
    monkeypatch.setenv("GOOGLE_TEXT_SEARCH_MONTHLY_LIMIT", "999999")
    monkeypatch.setenv("GOOGLE_PLACE_DETAILS_MONTHLY_LIMIT", "999999")
    monkeypatch.setenv("GOOGLE_MAX_TEXT_SEARCH_PER_JOB", "500")
    s = load_settings()
    assert (s.google_text_search_monthly_limit, s.google_details_monthly_limit) == (5000, 10000)
    assert s.google_max_text_search_per_job == 50


def test_requests_and_estimated_cost_are_logged(db, gsettings):
    usage = UsageLimiter(db, limits={TEXT_SEARCH: 10, DETAILS: 10}, free_caps={TEXT_SEARCH: 1, DETAILS: 10},
                         clock=Clock(datetime(2026, 11, 13, tzinfo=timezone.utc)))
    for _ in range(2):
        res = usage.reserve(TEXT_SEARCH)
        usage.record(res, ok=True, http_status=200)
    logs = list(db.apiUsageLog.find().sort("counter", 1))
    assert [entry["estimatedCostUsd"] for entry in logs] == [0.0, 0.032]  # 2nd is past the (test) free cap
    assert db.apiUsage.find_one()["estimatedCostUsd"] == pytest.approx(0.032)


class SerializedCollection:
    """Each operation is applied whole, one at a time, like MongoDB does for a single document.
    (mongomock has no locking, so without this the TEST would race, not the code under test.)"""

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


def test_concurrent_reservations_never_exceed_the_limit(db, gsettings):
    lock = threading.Lock()
    limiters = []
    for _ in range(8):  # 8 "workers", each with its own limiter object
        u = usage_for(db, gsettings, google_text_search_monthly_limit=100)
        u.coll = SerializedCollection(db.apiUsage, lock)
        limiters.append(u)
    granted = []

    def worker(u):
        for _ in range(40):
            if u.reserve(TEXT_SEARCH):
                granted.append(1)

    threads = [threading.Thread(target=worker, args=(u,)) for u in limiters]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(granted) == 100 and db.apiUsage.find_one()[TEXT_SEARCH] == 100


# ---------------------------------------------------------------- subdivision + coverage


def crowded(n=200, prefix="Crowd"):
    """n gyms spread over London (more than one query's 60-result cap)."""
    out = []
    for i in range(n):
        lat = 51.30 + (i % 20) * 0.019
        lon = -0.50 + (i // 20) * 0.08
        out.append(gplace(f"ChIJcrowd{i:05d}", f"{prefix} Gym {i}", lat, lon))
    return out


def test_full_tile_is_split_and_coverage_remembered(db, gsettings):
    fake = FakeGoogle(crowded())
    found = discovery.discover(places_client(db, gsettings, fake), db, owner_id=OWNER, categories=["gym"],
                               bbox=LONDON_BBOX, max_requests=50, now=q.now())
    assert found.tiles_split >= 1
    assert len(found.candidates) > 60  # more than one query could ever return
    assert found.requests <= 50
    root = db.googleCoverage.find_one({"bbox": LONDON_BBOX})
    assert root["saturated"] is True and root["results"] == 60
    assert "name" not in json.dumps(list(db.googleCoverage.find()), default=str).lower().replace("ownerid", "")


def test_per_job_budget_is_respected(db, gsettings):
    fake = FakeGoogle(crowded(400))
    found = discovery.discover(places_client(db, gsettings, fake), db, owner_id=OWNER, categories=["gym"],
                               bbox=LONDON_BBOX, max_requests=7, now=q.now())
    assert found.requests == 7 and len(fake.text_calls()) == 7 and found.stopped == "budget"


def test_repeat_search_goes_deeper_and_finds_new_places(db, gsettings):
    fake = FakeGoogle(crowded(400))
    first = discovery.discover(places_client(db, gsettings, fake), db, owner_id=OWNER, categories=["gym"],
                               bbox=LONDON_BBOX, max_requests=9, now=q.now())
    calls_before = len(fake.text_calls())
    second = discovery.discover(places_client(db, gsettings, fake), db, owner_id=OWNER, categories=["gym"],
                                bbox=LONDON_BBOX, max_requests=9, now=q.now())
    assert second.tiles_skipped >= 1  # the root tile isn't paid for again
    new = set(second.candidates) - set(first.candidates)
    assert len(new) > 0
    first_bodies = {c.content for c in fake.text_calls()[:calls_before]}
    assert not any(c.content in first_bodies for c in fake.text_calls()[calls_before:])  # no repeated request


def test_quota_exhausted_mid_search_stops_cleanly(db, gsettings):
    fake = FakeGoogle(crowded())
    usage = usage_for(db, gsettings, google_text_search_monthly_limit=2)
    found = discovery.discover(places_client(db, gsettings, fake, usage), db, owner_id=OWNER, categories=["gym"],
                               bbox=LONDON_BBOX, max_requests=50, now=q.now())
    assert found.stopped == "quota" and len(fake.text_calls()) == 2


# ---------------------------------------------------------------- full job: Google + OSM


def boxing(id_, name, lat=51.5, lon=-0.1, **tags):
    return {"type": "node", "id": id_, "lat": lat, "lon": lon, "tags": {"name": name, "sport": "boxing", **tags}}


OSM_ELEMENTS = [
    boxing(1, "Iron Fist", 51.5000, -0.1000, website="https://ironfist.test", **{"addr:postcode": "N1 1AA"}),
    boxing(2, "Southpaw Club", 51.5200, -0.1300),
]
GOOGLE_PLACES = [
    gplace("ChIJironfist001", "Iron Fist Boxing Gym", 51.50015, -0.10010, "N1 1AA"),  # same as OSM node/1 (~15 m)
    gplace("ChIJgoogleonly1", "Secret Squirrel Boxing", 51.4800, -0.0500),  # only on Google
    gplace("ChIJclosed00001", "Old Gym", 51.49, -0.09, status="CLOSED_PERMANENTLY"),
]


def google_job(db, gsettings, osm_elements=OSM_ELEMENTS, google=None, usage=None, sources=("osm", "google"), web=None):
    google = google or FakeGoogle(GOOGLE_PLACES)
    web = web or FakeWeb()
    add_job(db, sources=list(sources), categories=["boxing_club"])
    job = q.claim_next_job(db.scrapingJobs, "w1")
    status = run_job(db, job, gsettings, client=FakeOSM(elements=osm_elements).client(gsettings),
                     sleep=lambda _s: None, limiter=__import__("app.osm.ratelimit", fromlist=["x"]).RateLimiter(0),
                     web_transport=web.transport(), mx_resolve=web.resolve, google_transport=google.transport(),
                     usage=usage or usage_for(db, gsettings))
    return status, db.scrapingJobs.find_one({"_id": job["_id"]}), google


def test_google_results_are_never_linked_to_leads_and_no_google_content_is_stored(db, gsettings):
    status, job, google = google_job(db, gsettings)

    assert status == "completed"
    g = job["google"]
    assert g["mode"] == "google+osm" and g["placesFound"] == 2 and g["newPlaces"] == 2  # closed place skipped
    assert db.leads.count_documents({}) == 2  # only the OSM leads; Google never creates a lead
    # Decision 2026-11-14: no matching. "Iron Fist Boxing Gym" 15 m from OSM "Iron Fist" is NOT linked.
    for lead in db.leads.find():
        assert "googlePlaceId" not in lead.get("externalIds", {}) and "googleMatch" not in lead
        assert not any(s.get("type") == "google" for s in lead.get("sources", []))
        assert lead["jobIds"] == [job["_id"]]  # Google didn't attach itself to the lead's history either
    refs = {r["placeId"]: r for r in db.googlePlaceRefs.find()}
    assert set(refs) == {"ChIJironfist001", "ChIJgoogleonly1"}
    for ref in refs.values():
        assert ref["status"] == "seen" and "leadId" not in ref and "location" not in ref
        assert set(ref) <= {"_id", "ownerId", "placeId", "status", "area", "lastSeenAt", "lastJobId", "firstSeenAt", "jobIds"}

    # Compliance: no Google name, address or coordinate is stored anywhere in the database.
    everything = json.dumps({c: list(db[c].find()) for c in db.list_collection_names()}, default=str)
    for google_text in ("Secret Squirrel", "Iron Fist Boxing Gym", "London N1 1AA, UK", "51.50015", "-0.0500"):
        assert google_text not in everything
    assert google.detail_calls() == []  # Place Details is no longer used at all


def test_rerun_with_google_stores_no_duplicate_refs_and_pays_nothing_twice(db, gsettings):
    google_job(db, gsettings)
    status, job, google = google_job(db, gsettings)
    assert job["stats"]["newLeads"] == 0 and db.leads.count_documents({}) == 2
    assert db.googlePlaceRefs.count_documents({}) == 2
    assert job["google"]["textSearchRequests"] == 0  # whole area covered last time: nothing paid twice

    db.googleCoverage.delete_many({})  # 30 days later the area is searched again
    _, job, _ = google_job(db, gsettings)
    assert job["google"]["placesFound"] == 2 and job["google"]["newPlaces"] == 0
    assert db.googlePlaceRefs.count_documents({}) == 2  # unique per (user, place ID)
    assert len(db.googlePlaceRefs.find_one({"placeId": "ChIJgoogleonly1"})["jobIds"]) == 2



def test_limit_reached_switches_job_to_osm_only(db, gsettings):
    usage = usage_for(db, gsettings, google_text_search_monthly_limit=1)
    assert usage.reserve(TEXT_SEARCH)  # another job used the last unit
    status, job, google = google_job(db, gsettings, usage=usage)
    assert status == "completed"
    assert job["google"]["mode"] == "osm-only" and "Monthly Google limit" in job["google"]["note"]
    assert google.calls == []  # not a single Google request
    assert job["stats"]["newLeads"] == 2  # OSM worked as usual


def test_no_key_runs_osm_only(db, settings):
    status, job, google = google_job(db, settings)
    assert job["google"]["mode"] == "osm-only" and google.calls == []
    assert db.leads.count_documents({}) == 2


def test_job_without_google_source_never_calls_google(db, gsettings):
    status, job, google = google_job(db, gsettings, sources=("osm",))
    assert google.calls == [] and job["google"]["mode"] == "off"


def test_google_error_keeps_osm_results(db, gsettings):
    status, job, _ = google_job(db, gsettings, google=FakeGoogle(status=403))
    assert job["google"]["mode"] == "osm-only" and "403" in job["google"]["note"]
    assert db.leads.count_documents({}) == 2 and status == "partial"


def test_concurrent_jobs_share_the_monthly_limit(db, gsettings):
    """Two jobs (different users) at the same time with only 5 units left in the month."""
    from bson import ObjectId
    lock = threading.Lock()
    usage = usage_for(db, gsettings, google_text_search_monthly_limit=5)
    usage.coll = SerializedCollection(db.apiUsage, lock)
    owners = [ObjectId(), ObjectId()]
    for o in owners:
        add_job(db, ownerId=o, sources=["osm", "google"], categories=["gym"])
    jobs = [q.claim_next_job(db.scrapingJobs, "w1"), q.claim_next_job(db.scrapingJobs, "w2")]
    fake = FakeGoogle(crowded())
    results = {}

    def run(job):
        results[job["_id"]] = run_job(
            db, job, gsettings, client=FakeOSM(elements=[]).client(gsettings), sleep=lambda _s: None,
            limiter=__import__("app.osm.ratelimit", fromlist=["x"]).RateLimiter(0), web_transport=FakeWeb().transport(),
            google_transport=fake.transport(), usage=usage)

    threads = [threading.Thread(target=run, args=(j,)) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(results.values()) == {"completed"}
    assert len(fake.text_calls()) == 5 and db.apiUsage.find_one()[TEXT_SEARCH] == 5  # never more than the limit
    reports = [db.scrapingJobs.find_one({"_id": j["_id"]})["google"] for j in jobs]
    assert sum(r.get("textSearchRequests", 0) for r in reports) == 5
    assert all(r["mode"] in ("google+osm", "osm-only") for r in reports)
    assert any(r["mode"] == "osm-only" for r in reports)  # whoever ran out switched to OSM-only


# ---------------------------------------------------------------- leftovers from the earlier version


def test_old_cached_coordinates_are_still_purged_after_30_days(db):
    from datetime import timedelta as td
    db.googlePlaceRefs.insert_one({"ownerId": OWNER, "placeId": "ChIJold000001", "status": "unmatched",
                                   "location": {"lat": 51.5, "lon": -0.1}, "locationExpiresAt": q.now() - td(days=1)})
    assert merge.purge_expired_locations(db, q.now()) == 1
    ref = db.googlePlaceRefs.find_one()
    assert "location" not in ref and ref["placeId"] == "ChIJold000001"  # the ID itself may be kept



# ---------------------------------------------------------------- Retry never re-runs Google


@pytest.mark.parametrize("sources", [["osm", "google"], ["google"], ["google", "osm"]])
def test_job_created_by_retry_never_calls_google_even_if_sources_say_so(db, gsettings, sources):
    """Second line of defense: the web app strips "google" on Retry, and the worker also refuses it."""
    from bson import ObjectId

    google = FakeGoogle(GOOGLE_PLACES)
    add_job(db, sources=sources, categories=["boxing_club"], retryOf=ObjectId())
    job = q.claim_next_job(db.scrapingJobs, "w1")
    status = run_job(db, job, gsettings, client=FakeOSM(elements=OSM_ELEMENTS).client(gsettings),
                     sleep=lambda _s: None, limiter=__import__("app.osm.ratelimit", fromlist=["x"]).RateLimiter(0),
                     web_transport=FakeWeb().transport(), google_transport=google.transport(),
                     usage=usage_for(db, gsettings))
    saved = db.scrapingJobs.find_one({"_id": job["_id"]})
    assert status == "completed"
    assert google.calls == []  # not a single Google request
    assert saved["google"]["mode"] == "off"
    assert db.apiUsage.count_documents({}) == 0 and db.googlePlaceRefs.count_documents({}) == 0
    assert db.leads.count_documents({}) == 2  # OSM still works


def test_original_job_that_asked_for_google_still_uses_it(db, gsettings):
    """Control: only Retry is blocked; a job the user just created with Google ticked uses Google."""
    _, job, google = google_job(db, gsettings)
    assert job["google"]["mode"] == "google+osm" and len(google.text_calls()) > 0
