import httpx
import pytest

from app import jobs as q
from app import suppression
from app.main import process
from app.pipeline import JobCancelled, run_job
from tests.conftest import OWNER, FakeOSM, FakeWeb, add_job


def boxing(id_, name, **tags):
    return {"type": "node", "id": id_, "lat": 51.5, "lon": -0.1, "tags": {"name": name, "sport": "boxing", **tags}}


ELEMENTS = [
    boxing(1, "Iron Fist", website="https://ironfist.co.uk", email="info@ironfist.co.uk"),
    boxing(2, "Southpaw Club", phone="+44 20 7946 0000"),
    boxing(3, "Chain Gym Leeds", website="https://chaingym.com/leeds"),
    boxing(4, "Chain Gym York", website="https://chaingym.com/york"),  # same domain, different branch
]


def claim_and_run(db, settings, fake, limiter, web=None, **job_over):
    web = web or FakeWeb()
    add_job(db, **job_over)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    status = run_job(db, job, settings, client=fake.client(settings), sleep=lambda _s: None, limiter=limiter,
                     web_transport=web.transport(), mx_resolve=web.resolve)
    return job["_id"], status, db.scrapingJobs.find_one({"_id": job["_id"]})


def test_end_to_end_saves_leads_with_sources(db, settings, no_wait_limiter):
    fake = FakeOSM(elements=ELEMENTS)
    job_id, status, job = claim_and_run(db, settings, fake, no_wait_limiter)

    assert status == "completed"
    assert job["location"]["displayName"].startswith("London")
    assert job["location"]["countryCode"] == "GB"
    assert job["stats"]["discovered"] == 4 and job["stats"]["newLeads"] == 4 and job["stats"]["withWebsite"] == 3
    assert job["progress"]["pct"] == 100
    assert job["stats"]["websitesChecked"] == 3  # 3 different website URLs (the fake sites are all down)
    assert job["stats"]["websitesReachable"] == 0

    leads = {d["name"]: d for d in db.leads.find()}
    assert len(leads) == 4  # chain branches are separate leads
    iron = leads["Iron Fist"]
    assert iron["ownerId"] == OWNER and iron["jobIds"] == [job_id]
    assert iron["emails"][0]["sourceUrl"] == "https://www.openstreetmap.org/node/1"
    assert iron["saved"] is False and iron["status"] == "new"

    ua = fake.calls[0].headers["user-agent"]
    assert settings.crawler_contact_url in ua
    assert "(51.28,-0.51,51.69,0.33)" in fake.queries()[0]  # searched the chosen place's area (offline boundary)
    assert job["location"]["displayName"] == "London, England, United Kingdom"
    assert job["location"]["boundary"]["source"] == "naturalearth-urban-area"
    assert job["location"]["boundary"]["version"] == "test-urban"  # exact boundary version recorded


def test_rerun_updates_instead_of_duplicating_and_keeps_user_fields(db, settings, no_wait_limiter):
    claim_and_run(db, settings, FakeOSM(elements=ELEMENTS), no_wait_limiter)
    db.leads.update_one({"externalIds.osm": "node/1"}, {"$set": {"saved": True, "notes": "Call Monday", "status": "contacted"}})
    # a website email found by the crawler (Step 3) must survive an OSM refresh
    db.leads.update_one({"externalIds.osm": "node/1"}, {"$push": {"emails": {
        "value": "coach@ironfist.co.uk", "kind": "named", "sourceUrl": "https://ironfist.co.uk/contact", "sourceType": "website"}}})

    changed = [boxing(1, "Iron Fist Boxing", website="https://ironfist.co.uk", email="hello@ironfist.co.uk"), *ELEMENTS[1:]]
    job_id, status, job = claim_and_run(db, settings, FakeOSM(elements=changed), no_wait_limiter)

    assert status == "completed"
    assert job["stats"]["newLeads"] == 0 and job["stats"]["updated"] == 4
    assert db.leads.count_documents({}) == 4
    iron = db.leads.find_one({"externalIds.osm": "node/1"})
    assert iron["name"] == "Iron Fist Boxing"
    assert (iron["saved"], iron["notes"], iron["status"]) == (True, "Call Monday", "contacted")
    assert {e["value"] for e in iron["emails"]} == {"coach@ironfist.co.uk", "hello@ironfist.co.uk"}  # old OSM email replaced
    assert len(iron["jobIds"]) == 2 and iron["jobIds"][1] == job_id


def test_no_external_geocoding_and_places_outside_the_area_are_dropped(db, settings, no_wait_limiter):
    # Returned by the map search, but outside London's outline (north of it).
    outside = {"type": "node", "id": 9, "lat": 51.75, "lon": -0.40, "tags": {"name": "Watford Boxing", "sport": "boxing"}}
    fake = FakeOSM(elements=[*ELEMENTS, outside])
    _, status, job = claim_and_run(db, settings, fake, no_wait_limiter)
    assert status == "completed"
    assert fake.count("nominatim.test") == 0  # Nominatim is never called
    assert job["stats"]["outsideArea"] == 1 and db.leads.count_documents({}) == 4
    assert db.leads.count_documents({"externalIds.osm": "node/9"}) == 0
    assert all(d["address"]["countryCode"] == "GB" for d in db.leads.find({"address": {"$exists": True}}))


def test_job_without_a_picked_place_fails_with_a_clear_message(db, settings, no_wait_limiter):
    from app.main import process

    add_job(db, location={"query": "London, UK"})  # created before place selection existed
    job = q.claim_next_job(db.scrapingJobs, "w1")
    process(db, job, settings)
    saved = db.scrapingJobs.find_one({"_id": job["_id"]})
    assert saved["status"] == "failed"  # permanent: retrying can't fix it
    assert "pick the place" in saved["progress"]["stage"]


def test_suppressed_businesses_are_not_saved(db, settings, no_wait_limiter):
    db.suppressionList.insert_many([
        {"ownerId": OWNER, "type": "email", "valueHash": suppression.value_hash("email", "INFO@ironfist.co.uk ")},
        {"ownerId": OWNER, "type": "domain", "valueHash": suppression.value_hash("domain", "www.chaingym.com")},
        {"ownerId": OWNER, "type": "phone", "valueHash": suppression.value_hash("phone", "+44 (20) 7946-0000")},
    ])
    _, status, job = claim_and_run(db, settings, FakeOSM(elements=ELEMENTS), no_wait_limiter)
    assert status == "completed"
    assert job["stats"]["suppressed"] == 4 and job["stats"]["newLeads"] == 0
    assert db.leads.count_documents({}) == 0


def test_other_owners_suppression_does_not_apply(db, settings, no_wait_limiter):
    from bson import ObjectId

    db.suppressionList.insert_one({"ownerId": ObjectId(), "type": "domain", "valueHash": suppression.value_hash("domain", "ironfist.co.uk")})
    _, _, job = claim_and_run(db, settings, FakeOSM(elements=ELEMENTS), no_wait_limiter)
    assert job["stats"]["suppressed"] == 0


def test_cancel_during_job_stops_it(db, settings, no_wait_limiter):
    add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    db.scrapingJobs.update_one({"_id": job["_id"]}, {"$set": {"cancelRequested": True}})
    with pytest.raises(JobCancelled):
        run_job(db, job, settings, client=FakeOSM(elements=ELEMENTS).client(), limiter=no_wait_limiter)
    assert db.leads.count_documents({}) == 0


def test_one_bad_element_makes_job_partial(db, settings, no_wait_limiter):
    broken = {"type": "node", "tags": {"name": "No id"}}  # missing "id" -> KeyError while mapping
    _, status, job = claim_and_run(db, settings, FakeOSM(elements=[*ELEMENTS, broken]), no_wait_limiter)
    assert status == "partial"
    assert job["stats"]["newLeads"] == 4 and job["stats"]["errors"] == 1
    assert job["errorLog"][0]["stage"] == "save"


def test_progress_moves_while_saving_a_big_piece(db, settings, no_wait_limiter, monkeypatch):
    from app import pipeline

    seen_pct = []
    real_heartbeat = q.heartbeat

    def spy(jobs, job_id, stage=None, pct=None, **kw):
        if pct is not None:
            seen_pct.append(pct)
        return real_heartbeat(jobs, job_id, stage=stage, pct=pct, **kw)

    monkeypatch.setattr(pipeline.q, "heartbeat", spy)
    many = [boxing(i, f"Club {i}") for i in range(1, 101)]
    _, status, job = claim_and_run(db, settings, FakeOSM(elements=many), no_wait_limiter)
    assert status == "completed" and job["stats"]["newLeads"] == 100
    saving = [p for p in seen_pct if 10 < p < 100]
    assert len(saving) >= 3 and saving == sorted(saving)  # bar moves up while saving


def test_no_results_is_a_completed_job(db, settings, no_wait_limiter):
    _, status, job = claim_and_run(db, settings, FakeOSM(elements=[]), no_wait_limiter)
    assert status == "completed" and job["stats"]["discovered"] == 0


def test_friendly_failure_reaches_the_job(db, settings, no_wait_limiter, monkeypatch):
    from app import pipeline

    fake = FakeOSM(overpass_status=503)  # always busy
    real_make_client = pipeline.make_client
    monkeypatch.setattr(pipeline, "make_client", lambda s: real_make_client(s, transport=httpx.MockTransport(fake.handler)))
    monkeypatch.setattr(pipeline, "NOMINATIM_LIMITER", no_wait_limiter)
    monkeypatch.setattr(pipeline.time, "sleep", lambda _s: None)
    add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    process(db, job, settings)
    saved = db.scrapingJobs.find_one({"_id": job["_id"]})
    # A busy map server is temporary: the job waits and retries (it used to fail at once).
    assert saved["status"] == "queued" and saved["nextAttemptAt"] > q.now()
    assert "busy" in saved["progress"]["stage"] and "retrying" in saved["progress"]["stage"]

    # Still busy on the last allowed attempt: now it fails, with the friendly message.
    db.scrapingJobs.update_one({"_id": job["_id"]}, {"$set": {"nextAttemptAt": q.now(), "attempts": settings.max_attempts - 1}})
    job = q.claim_next_job(db.scrapingJobs, "w1")
    process(db, job, settings)
    saved = db.scrapingJobs.find_one({"_id": job["_id"]})
    assert saved["status"] == "failed"
    assert "busy" in saved["progress"]["stage"]


def test_missing_contact_url_fails_before_any_request(db, settings, no_wait_limiter):
    from dataclasses import replace

    fake = FakeOSM()
    add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    with pytest.raises(RuntimeError, match="CRAWLER_CONTACT_URL"):
        run_job(db, job, replace(settings, crawler_contact_url=""), client=fake.client(), limiter=no_wait_limiter)
    assert fake.calls == []


def test_cancel_is_noticed_while_waiting_for_a_busy_map_server(db, settings, no_wait_limiter):
    add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    waits = []

    def sleep(seconds):  # the user presses Cancel during the first "server busy" pause
        waits.append(seconds)
        db.scrapingJobs.update_one({"_id": job["_id"]}, {"$set": {"cancelRequested": True}})

    fake = FakeOSM(overpass_status=503)
    with pytest.raises(JobCancelled):
        run_job(db, job, settings, client=fake.client(settings), sleep=sleep, limiter=no_wait_limiter,
                web_transport=FakeWeb().transport(), mx_resolve=FakeWeb().resolve)
    assert len(waits) == 1 and fake.count("overpass.test") == 1  # stopped right away, no more retries


def test_heartbeat_keeps_a_long_wait_alive(db, settings, no_wait_limiter):
    add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    old = q.now().replace(year=2020)
    beats = []

    def sleep(_seconds):  # pretend a lot of time passed; the next heartbeat must refresh lockedAt
        db.scrapingJobs.update_one({"_id": job["_id"]}, {"$set": {"lockedAt": old}})
        beats.append(1)

    fake = FakeOSM(overpass_responses=[httpx.Response(503)], elements=[])
    run_job(db, job, settings, client=fake.client(settings), sleep=sleep, limiter=no_wait_limiter,
            web_transport=FakeWeb().transport(), mx_resolve=FakeWeb().resolve)
    assert beats and db.scrapingJobs.find_one({"_id": job["_id"]})["lockedAt"] > old
