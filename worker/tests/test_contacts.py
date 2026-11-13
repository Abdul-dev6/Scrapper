"""Contact check statuses: automatic checks are never "verified"; a person's verification survives refreshes."""

import pytest

from app import contacts, leads
from app import jobs as q
from app.osm.mapping import element_to_lead
from app.pipeline import run_job
from tests.conftest import OWNER, FakeOSM, FakeWeb, add_job
from tests.test_crawl import CONTACT, HOME

E = lambda v, **c: {"value": v, "checks": c} if c else {"value": v}  # noqa: E731
P = lambda e164=None, raw="x", **c: {"e164": e164, "raw": raw, "checks": c}  # noqa: E731


@pytest.mark.parametrize("emails,phones,whatsapp,expected", [
    ([], [], [], "none"),
    ([E("a@b.test", syntaxValid=True, mx="unknown")], [], [], "unchecked"),
    ([E("a@b.test", syntaxValid=True, mx="no_mx")], [], [], "unchecked"),
    ([E("a@b.test", syntaxValid=True, mx="valid")], [], [], "checked"),
    ([], [P("+442079460000", formatValid=True)], [], "checked"),
    ([], [P(None, raw="123", formatValid=False)], [], "unchecked"),
    ([], [], [{"e164": "+447911123456"}], "checked"),
    ([E("a@b.test", syntaxValid=True, mx="no_mx", manuallyVerified=True)], [], [], "verified"),
    ([E("a@b.test", mx="valid")], [P("+442079460000", formatValid=True, manuallyVerified=True)], [], "verified"),
    ([{"value": "legacy@b.test", "mxValid": True}], [], [], "checked"),  # old records before the migration
])
def test_contact_status_rules(emails, phones, whatsapp, expected):
    assert contacts.contact_status(emails, phones, whatsapp) == expected


def test_syntax_and_mx_valid_is_never_verified():
    best_machine_result = [E("a@b.test", syntaxValid=True, mx="valid")]
    best_phone = [P("+442079460000", formatValid=True)]
    assert contacts.contact_status(best_machine_result, best_phone) != "verified"


def test_new_checks_keep_manual_decision():
    prev = {"syntaxValid": True, "mx": "unknown", "manuallyVerified": True, "manuallyVerifiedAt": "t", "manuallyVerifiedBy": "u"}
    checks = contacts.email_checks(prev, mx=False, now=q.now())
    assert checks["mx"] == "no_mx" and checks["manuallyVerified"] is True and checks["manuallyVerifiedBy"] == "u"


SITE = {"https://ironfist.test/": HOME, "https://ironfist.test/contact": CONTACT}


def _run(db, settings, limiter, elements, web):
    add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    run_job(db, job, settings, client=FakeOSM(elements=elements).client(settings), sleep=lambda _s: None,
            limiter=limiter, web_transport=web.transport(), mx_resolve=web.resolve)


def _mark(db, field, key, value):
    lead = db.leads.find_one()
    for item in lead[field]:
        if item.get(key) == value:
            item["checks"] = {**item.get("checks", {}), "manuallyVerified": True, "manuallyVerifiedBy": "user-1"}
    db.leads.update_one({"_id": lead["_id"]}, {"$set": {field: lead[field], "contactStatus": "verified"}})


def test_manual_verification_survives_website_recrawl(db, settings, no_wait_limiter):
    el = [{"type": "node", "id": 1, "lat": 51.5, "lon": -0.1,
           "tags": {"name": "Iron Fist", "sport": "boxing", "website": "https://ironfist.test/"}}]
    _run(db, settings, no_wait_limiter, el, FakeWeb(pages=SITE))
    _mark(db, "emails", "value", "coach.mike@ironfist.test")
    _mark(db, "phones", "e164", "+442079460001")
    db.leads.update_one({}, {"$set": {"website.checkedAt": q.now().replace(year=2020)}})  # due for a re-check

    _run(db, settings, no_wait_limiter, el, FakeWeb(pages=SITE))  # website values are rebuilt from scratch
    lead = db.leads.find_one()
    coach = next(e for e in lead["emails"] if e["value"] == "coach.mike@ironfist.test")
    phone = next(p for p in lead["phones"] if p["e164"] == "+442079460001")
    assert coach["checks"]["manuallyVerified"] is True and coach["checks"]["manuallyVerifiedBy"] == "user-1"
    assert phone["checks"]["manuallyVerified"] is True
    assert lead["contactStatus"] == "verified"


def test_manual_verification_survives_osm_refresh(db):
    el = {"type": "node", "id": 7, "lat": 51.5, "lon": -0.1,
          "tags": {"name": "Gym", "sport": "boxing", "email": "info@gym.test", "phone": "+44 20 7946 0000"}}
    leads.upsert_lead(db.leads, owner_id=OWNER, job_id="j1", lead=element_to_lead(el, now=q.now()), now=q.now())
    _mark(db, "emails", "value", "info@gym.test")
    leads.upsert_lead(db.leads, owner_id=OWNER, job_id="j2", lead=element_to_lead(el, now=q.now()), now=q.now())
    lead = db.leads.find_one()
    assert lead["emails"][0]["checks"]["manuallyVerified"] is True
    assert lead["contactStatus"] == "verified"


def test_osm_phone_gets_format_check_on_save(db):
    el = {"type": "node", "id": 8, "lat": 51.5, "lon": -0.1,
          "tags": {"name": "Gym", "sport": "boxing", "phone": "020 7946 0000", "addr:country": "GB"}}
    leads.upsert_lead(db.leads, owner_id=OWNER, job_id="j1", lead=element_to_lead(el, now=q.now()), now=q.now())
    lead = db.leads.find_one()
    assert lead["phones"][0]["checks"]["formatValid"] is True
    assert lead["contactStatus"] == "checked"


def test_verification_made_during_a_crawl_is_not_overwritten(db, settings, no_wait_limiter, monkeypatch):
    from app.crawl import runner

    el = [{"type": "node", "id": 1, "lat": 51.5, "lon": -0.1,
           "tags": {"name": "Iron Fist", "sport": "boxing", "website": "https://ironfist.test/",
                    "email": "osm@ironfist.test"}}]
    real = runner.build_update
    calls = []

    async def racing_build_update(lead, result, **kw):
        if not calls:  # a person verifies the OSM email while the crawler is working on its stale copy
            _mark(db, "emails", "value", "osm@ironfist.test")
            db.leads.update_one({"_id": lead["_id"]}, {"$set": {"updatedAt": q.now()}})
        calls.append(1)
        return await real(lead, result, **kw)

    monkeypatch.setattr(runner, "build_update", racing_build_update)
    _run(db, settings, no_wait_limiter, el, FakeWeb(pages=SITE))
    lead = db.leads.find_one()
    osm_email = next(e for e in lead["emails"] if e["value"] == "osm@ironfist.test")
    assert osm_email["checks"]["manuallyVerified"] is True  # the person's decision survived
    assert any(e["value"] == "coach.mike@ironfist.test" for e in lead["emails"])  # and the crawl was saved
    assert len(calls) == 2  # rebuilt once from the fresh copy
