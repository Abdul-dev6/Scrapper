"""Whole job: discovery -> website crawl -> merged, verified leads."""

from app import jobs as q
from app import suppression
from app.pipeline import run_job
from tests.conftest import OWNER, FakeOSM, FakeWeb, add_job
from tests.test_crawl import CONTACT, HOME


def place(id_, name, **tags):
    return {"type": "node", "id": id_, "lat": 51.5, "lon": -0.1, "tags": {"name": name, "sport": "boxing", **tags}}


SITE = {"https://ironfist.test/": HOME, "https://ironfist.test/contact": CONTACT}


def job_run(db, settings, limiter, elements, web):
    add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    status = run_job(db, job, settings, client=FakeOSM(elements=elements).client(settings), sleep=lambda _s: None,
                     limiter=limiter, web_transport=web.transport(), mx_resolve=web.resolve)
    return status, db.scrapingJobs.find_one({"_id": job["_id"]})


def test_website_contacts_are_added_with_sources(db, settings, no_wait_limiter):
    web = FakeWeb(pages=SITE)
    status, job = job_run(db, settings, no_wait_limiter,
                          [place(1, "Iron Fist", website="https://ironfist.test/", email="osm@ironfist.test")], web)
    assert status == "completed"
    lead = db.leads.find_one({"externalIds.osm": "node/1"})

    emails = {e["value"]: e for e in lead["emails"]}
    assert emails["osm@ironfist.test"]["sourceType"] == "osm"  # OSM value kept
    coach = dict(emails["coach.mike@ironfist.test"])
    checks = coach.pop("checks")
    assert coach == {
        "value": "coach.mike@ironfist.test", "kind": "named", "mxValid": True,
        "sourceUrl": "https://ironfist.test/contact", "sourceType": "website",
    }
    assert (checks["syntaxValid"], checks["mx"], checks["manuallyVerified"]) == (True, "valid", False)
    assert not any("wixpress" in v or "@2x" in v for v in emails)

    phones = {p["e164"]: p for p in lead["phones"]}
    assert set(phones) == {"+442079460000", "+442079460001"}
    assert phones["+442079460000"]["sourceUrl"] == "https://ironfist.test/"
    assert lead["whatsapp"][0]["e164"] == "+447911123456"
    assert lead["socials"]["instagram"]["url"] == "https://www.instagram.com/ironfistboxing"
    assert lead["socials"]["linkedin"]["sourceUrl"] == "https://ironfist.test/contact"
    assert lead["website"]["reachable"] is True and lead["website"]["checkedAt"]
    # Machine checks passed, but no person verified anything: "checked", never "verified".
    assert lead["contactStatus"] == "checked" and lead["confidence"] == 1.0
    assert {s["url"] for s in lead["sources"] if s["type"] == "website"} == set(SITE)

    assert job["stats"]["websitesChecked"] == 1 and job["stats"]["websitesReachable"] == 1
    assert job["stats"]["withEmail"] == 1
    assert job["progress"]["pct"] == 100


def test_chain_branches_sharing_a_website_are_crawled_once(db, settings, no_wait_limiter):
    web = FakeWeb(pages=SITE)
    elements = [place(1, "Iron Fist North", website="https://ironfist.test/"),
                place(2, "Iron Fist South", website="https://ironfist.test/")]
    job_run(db, settings, no_wait_limiter, elements, web)
    assert web.visited().count("https://ironfist.test/") == 1
    assert db.leads.count_documents({"emails.value": "coach.mike@ironfist.test"}) == 2  # both branches got it


def test_rerun_soon_does_not_crawl_again(db, settings, no_wait_limiter):
    elements = [place(1, "Iron Fist", website="https://ironfist.test/")]
    job_run(db, settings, no_wait_limiter, elements, FakeWeb(pages=SITE))
    web2 = FakeWeb(pages=SITE)
    _, job = job_run(db, settings, no_wait_limiter, elements, web2)
    assert web2.visited() == [] and job["stats"]["websitesChecked"] == 0
    lead = db.leads.find_one()
    assert any(e["sourceType"] == "website" for e in lead["emails"])  # kept from the first crawl


def test_site_down_keeps_old_website_data(db, settings, no_wait_limiter):
    elements = [place(1, "Iron Fist", website="https://ironfist.test/")]
    job_run(db, settings, no_wait_limiter, elements, FakeWeb(pages=SITE))
    db.leads.update_one({}, {"$set": {"website.checkedAt": q.now().replace(year=2020)}})  # make it "old"
    _, job = job_run(db, settings, no_wait_limiter, elements, FakeWeb())  # now everything is 404
    lead = db.leads.find_one()
    assert lead["website"]["reachable"] is False
    assert any(e["value"] == "coach.mike@ironfist.test" for e in lead["emails"])
    assert job["stats"]["websitesChecked"] == 1 and job["stats"]["websitesReachable"] == 0


def test_email_domain_without_mail_server_is_flagged(db, settings, no_wait_limiter):
    web = FakeWeb(pages={"https://gym.test/": '<a href="mailto:info@gym.test">Mail</a>'}, no_mx={"gym.test"})
    job_run(db, settings, no_wait_limiter, [place(1, "Gym", website="https://gym.test/")], web)
    lead = db.leads.find_one()
    assert lead["emails"][0]["mxValid"] is False
    assert lead["emails"][0]["checks"]["mx"] == "no_mx"
    assert lead["contactStatus"] == "unchecked"


def test_opted_out_contact_found_on_website_removes_the_lead(db, settings, no_wait_limiter):
    db.suppressionList.insert_one(
        {"ownerId": OWNER, "type": "email", "valueHash": suppression.value_hash("email", "coach.mike@ironfist.test")})
    elements = [place(1, "Iron Fist", website="https://ironfist.test/"), place(2, "Other", phone="+44 20 7946 0999")]
    _, job = job_run(db, settings, no_wait_limiter, elements, FakeWeb(pages=SITE))
    assert db.leads.count_documents({}) == 1 and db.leads.find_one()["name"] == "Other"
    assert job["stats"]["suppressed"] == 1


def test_osm_phones_get_international_format(db, settings, no_wait_limiter):
    job_run(db, settings, no_wait_limiter, [place(1, "Gym", phone="020 7946 0002", website="https://down.test/")],
            FakeWeb())
    assert db.leads.find_one()["phones"][0]["e164"] == "+442079460002"


def test_robots_blocked_site_is_not_visited(db, settings, no_wait_limiter):
    web = FakeWeb(pages=SITE, robots={"ironfist.test": "User-agent: *\nDisallow: /"})
    job_run(db, settings, no_wait_limiter, [place(1, "Iron Fist", website="https://ironfist.test/")], web)
    assert web.visited() == []
    assert db.leads.find_one()["website"]["reachable"] is False


def test_cancel_during_crawl_stops_the_job(db, settings, no_wait_limiter):
    import pytest

    from app.pipeline import JobCancelled

    pages = {f"https://gym{i}.test/": "<html></html>" for i in range(20)}
    elements = [place(i, f"Gym {i}", website=f"https://gym{i}.test/") for i in range(20)]
    web = FakeWeb(pages=pages)
    add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    original = web.handler

    def handler(request):  # the user presses Cancel while websites are being checked
        db.scrapingJobs.update_one({"_id": job["_id"]}, {"$set": {"cancelRequested": True}})
        return original(request)

    web.handler = handler
    with pytest.raises(JobCancelled):
        run_job(db, job, settings, client=FakeOSM(elements=elements).client(settings), sleep=lambda _s: None,
                limiter=no_wait_limiter, web_transport=web.transport(), mx_resolve=web.resolve)
    assert db.leads.count_documents({"website.checkedAt": {"$exists": True}}) < 20
