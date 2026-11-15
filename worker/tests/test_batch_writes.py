"""Batched Overture writes (leads.upsert_overture_batch): same results as the one-at-a-time path, fewer
round trips, and safe when something else writes at the same moment."""

from __future__ import annotations

import copy
from datetime import timedelta

import mongomock
import pytest
from bson import ObjectId

from app import contacts, leads
from app import jobs as q
from app.overture import mapping as ovm
from tests.conftest import OWNER

REL = "2026-10-21.0"
JOB = "job-b"


def row(gid, name, lat, lon, *, phone=None, website=None, record=None, postcode="SW1X 7HN", email=None):
    return {"id": gid, "name": name, "cat": "gym", "alts": None, "confidence": 0.9, "operating_status": None,
            "websites": [website] if website else None, "socials": None, "emails": [email] if email else None,
            "phones": [phone] if phone else None,
            "addresses": [{"freeform": "1 High Street", "locality": "London", "postcode": postcode, "region": "ENG",
                           "country": "GB"}],
            "sources": [{"dataset": "meta", "license": "CDLA-Permissive-2.0", "record_id": record or f"r-{gid}",
                         "update_time": "2026-10-12T00:00:00.000Z"}],
            "lat": lat, "lon": lon}


def lead_of(r):
    return ovm.row_to_lead(r, release=REL, now=q.now())


def indexed(db):
    """The unique indexes `npm run db:indexes` builds for leads."""
    for field in leads.PARTIAL_FIELDS:
        db.leads.create_index([("ownerId", 1), (field, 1)], unique=True,
                              partialFilterExpression={field: {"$type": "string"}})
    return db


def fresh_db():
    return indexed(mongomock.MongoClient(tz_aware=True).t)


# A realistic mix: duplicates by phone nearby, by source record, by same name+spot, the same row twice,
# chain branches far apart sharing a head-office phone, a branch with only a domain in common.
MIX = [
    row("g1", "Iron Fist Boxing", 51.5000, -0.1000, phone="+44 20 7946 0001", website="https://ironfist.test"),
    row("g2", "Iron Fist", 51.5004, -0.1003, phone="020 7946 0001"),  # same gym (phone, 50 m)
    row("g3", "Iron Fist Boxing", 51.5001, -0.1001, record="r-g1"),  # same upstream record as g1
    row("g4", "The Gym Group", 51.5200, -0.1200, phone="+44 300 303 4800"),
    row("g5", "The Gym Group", 51.4500, -0.0500, phone="+44 300 303 4800"),  # other branch, 7 km, central phone
    row("g6", "The Gym Group", 53.4800, -2.2400, phone="+44 300 303 4800"),  # Manchester
    row("g7", "Lone Wolf MMA", 51.5300, -0.1500, website="https://lonewolf.test"),
    row("g8", "Lone Wolf MMA Camden", 51.5400, -0.1400, website="https://lonewolf.test"),  # other branch
    row("g9", "Tiny Dojo", 51.5100, -0.1100),
    row("g10", "Tiny Dojo", 51.5102, -0.1101),  # same name, same spot (20 m)
]


def normalized(db):
    out = []
    for d in db.leads.find({}, {"_id": 0, "createdAt": 0, "updatedAt": 0, "discoveredAt": 0, "mergeLog.at": 0}):
        d.pop("sources", None)
        d.pop("fieldSources", None)
        for p in d.get("phones", []):
            p.get("checks", {}).pop("checkedAt", None)
        for e in d.get("emails", []):
            e.get("checks", {}).pop("checkedAt", None)
        d["externalIds"]["overtureAliases"] = sorted(d["externalIds"].get("overtureAliases", []))
        d["externalIds"]["overtureSources"] = sorted(d["externalIds"].get("overtureSources", []))
        out.append(d)
    return sorted(out, key=lambda d: d["externalIds"]["overture"])


class Counting:
    """A leads collection that counts round trips and can inject a concurrent write before the bulk write."""

    def __init__(self, coll, before_bulk=None):
        self.coll, self.before_bulk, self.calls = coll, before_bulk, []

    def __getattr__(self, name):
        attr = getattr(self.coll, name)
        if not callable(attr):
            return attr

        def wrapped(*a, **kw):
            self.calls.append(name)
            if name == "bulk_write" and self.before_bulk:
                hook, self.before_bulk = self.before_bulk, None
                hook(self.coll)
            return attr(*a, **kw)
        return wrapped


def batch(coll, rows, owner=OWNER, job=JOB):
    return leads.upsert_overture_batch(coll, owner_id=owner, job_id=job, items=[lead_of(r) for r in rows], now=q.now())


def single(coll, rows, owner=OWNER, job=JOB):
    return [leads.upsert_overture_lead(coll, owner_id=owner, job_id=job, lead=lead_of(r), now=q.now()) for r in rows]


def test_batch_gives_exactly_the_same_leads_as_one_at_a_time():
    a, b = fresh_db(), fresh_db()
    assert batch(b.leads, MIX) == single(a.leads, MIX)
    assert normalized(a) == normalized(b)
    # and again with the batch split differently, on top of existing data (a repeat search)
    assert single(a.leads, MIX[5:] + MIX[:5]) == batch(b.leads, MIX[5:] + MIX[:5])
    assert normalized(a) == normalized(b)


def test_batch_results_merges_and_branches():
    db = fresh_db()
    res = batch(db.leads, MIX)
    assert res == ["new", "merged", "updated", "new", "new", "new", "new", "new", "new", "merged"]
    assert db.leads.count_documents({}) == 7
    gym_group = list(db.leads.find({"nameNormalized": "the gym group"}))
    assert len(gym_group) == 3  # three branches sharing one central phone stay three leads
    iron = db.leads.find_one({"externalIds.overture": "g1"})
    assert set(iron["externalIds"]["overtureAliases"]) == {"g2", "g3"}
    assert len(iron["phones"]) == 1  # the same number written two ways


def test_clean_batch_uses_two_round_trips():
    db = fresh_db()
    coll = Counting(db.leads)
    batch(coll, MIX)
    assert coll.calls == ["find", "bulk_write"]
    coll.calls.clear()
    assert set(batch(coll, MIX)) == {"updated"}  # repeat search: found by id or alias, nothing new
    assert coll.calls == ["find", "bulk_write"] and db.leads.count_documents({}) == 7


def test_repeat_batch_adds_the_job_and_keeps_user_fields_and_manual_verification():
    db = fresh_db()
    batch(db.leads, [row("g1", "Iron Fist", 51.5, -0.1, phone="+44 20 7946 0001", email="info@ironfist.test")])
    lead = db.leads.find_one({})
    phones, emails = lead["phones"], lead["emails"]
    phones[0]["checks"].update(manuallyVerified=True, manuallyVerifiedAt=q.now(), manuallyVerifiedBy="u1")
    emails[0]["checks"].update(manuallyVerified=True, manuallyVerifiedAt=q.now(), manuallyVerifiedBy="u1")
    db.leads.update_one({"_id": lead["_id"]}, {"$set": {
        "phones": phones, "emails": emails, "saved": True, "status": "contacted", "notes": "call back"}})
    res = batch(db.leads, [row("g1", "Iron Fist", 51.5, -0.1, phone="020 7946 0001", email="info@ironfist.test")],
                job="job-2")
    assert res == ["updated"]
    after = db.leads.find_one({})
    assert after["saved"] is True and after["status"] == "contacted" and after["notes"] == "call back"
    assert after["phones"][0]["checks"]["manuallyVerified"] is True
    assert after["emails"][0]["checks"]["manuallyVerifiedBy"] == "u1"
    assert after["contactStatus"] == contacts.contact_status(after["emails"], after["phones"])
    assert set(after["jobIds"]) == {JOB, "job-2"}


def test_a_lead_changed_while_the_batch_runs_is_redone_from_the_fresh_copy():
    db = fresh_db()
    batch(db.leads, [row("g1", "Iron Fist", 51.5, -0.1, phone="+44 20 7946 0001")])

    def person_edits(coll):  # e.g. the web app saves a note (it bumps updatedAt)
        coll.update_one({}, {"$set": {"notes": "keep me", "updatedAt": q.now() + timedelta(seconds=5)}})
    coll = Counting(db.leads, before_bulk=person_edits)
    res = batch(coll, [row("g1", "Iron Fist Boxing", 51.5, -0.1, phone="+44 20 7946 0001"),
                       row("g9", "Tiny Dojo", 51.6, -0.2)])
    assert res == ["updated", "new"]
    lead = db.leads.find_one({"externalIds.overture": "g1"})
    assert lead["notes"] == "keep me" and lead["name"] == "Iron Fist Boxing"  # both changes kept
    assert db.leads.count_documents({}) == 2
    assert coll.calls.count("bulk_write") == 1 and "update_one" in coll.calls  # only g1 redone, one by one


def test_another_worker_inserting_the_same_place_first_is_handled():
    db = fresh_db()

    def other_worker(coll):
        leads.upsert_overture_lead(coll, owner_id=OWNER, job_id="job-other", lead=lead_of(row("g2", "B Gym", 52, -1)),
                                   now=q.now())
    coll = Counting(db.leads, before_bulk=other_worker)
    res = batch(coll, [row("g1", "A Gym", 51, -1), row("g2", "B Gym", 52, -1), row("g3", "C Gym", 53, -1)])
    assert res == ["new", "updated", "new"]  # g2 hit the unique index -> redone -> updates the other's lead
    assert sorted(d["externalIds"]["overture"] for d in db.leads.find()) == ["g1", "g2", "g3"]
    assert set(db.leads.find_one({"externalIds.overture": "g2"})["jobIds"]) == {"job-other", JOB}


def test_one_failing_lead_is_reported_and_the_others_are_saved(monkeypatch):
    db = fresh_db()

    def other_worker(coll):
        coll.insert_one({"ownerId": OWNER, "dataLane": "overture", "externalIds": {"overture": "g1"},
                         "updatedAt": q.now()})
    real = leads.upsert_overture_lead

    def broken(coll, **kw):
        if kw["lead"]["externalIds"]["overture"] == "g1":
            raise RuntimeError("boom")
        return real(coll, **kw)
    monkeypatch.setattr(leads, "upsert_overture_lead", broken)
    res = batch(Counting(db.leads, before_bulk=other_worker), [row("g1", "A", 51, -1), row("g2", "B", 52, -1)])
    assert isinstance(res[0], RuntimeError) and res[1] == "new"
    assert db.leads.count_documents({"externalIds.overture": "g2"}) == 1


def test_users_never_see_or_touch_each_others_leads():
    db = fresh_db()
    other = ObjectId()
    batch(db.leads, MIX, owner=other)
    before = list(db.leads.find({"ownerId": other}))
    res = batch(db.leads, MIX)
    assert res.count("new") == 7  # nothing of the other user's was matched
    assert list(db.leads.find({"ownerId": other})) == before


def test_never_merged_with_osm_leads_of_the_same_user():
    db = fresh_db()
    osm = {"ownerId": OWNER, "name": "Iron Fist Boxing", "nameNormalized": "iron fist boxing",
           "geo": {"type": "Point", "coordinates": [-0.1, 51.5]}, "website": {"url": "https://ironfist.test",
           "domain": "ironfist.test"}, "phones": [{"raw": "+44 20 7946 0001", "e164": "+442079460001"}],
           "externalIds": {"osm": "node/1", "overture": None}, "dedupeKey": "iron fist boxing|51.500|-0.100",
           "sources": [{"type": "osm", "url": "https://osm.org/node/1"}], "updatedAt": q.now()}
    db.leads.insert_one(copy.deepcopy(osm))
    res = batch(db.leads, [MIX[0]])
    assert res == ["new"]
    assert db.leads.count_documents({}) == 2
    stored_osm = db.leads.find_one({"externalIds.osm": "node/1"})
    assert "dataLane" not in stored_osm and "overtureAliases" not in stored_osm["externalIds"]
    assert db.leads.find_one({"dataLane": "overture"})["dedupeKey"].startswith("overture|")


def test_empty_batch_does_nothing():
    db = fresh_db()
    coll = Counting(db.leads)
    assert leads.upsert_overture_batch(coll, owner_id=OWNER, job_id=JOB, items=[], now=q.now()) == []
    assert coll.calls == []


@pytest.mark.parametrize("size", [1, 3, 7])
def test_any_batch_size_gives_the_same_result(size):
    a, b = fresh_db(), fresh_db()
    single(a.leads, MIX)
    for i in range(0, len(MIX), size):
        batch(b.leads, MIX[i:i + size])
    assert normalized(a) == normalized(b)
