"""Regression: lead lookups must use their indexes, never scan all of a user's leads.

MongoDB only uses a PARTIAL index (externalIds.osm / externalIds.overture / dedupeKey ...) when the query
implies the index filter {$type: "string"}; a plain {field: value} doesn't. Before the fix, every lookup
for a lead that wasn't saved yet examined all ~21,000 leads of the user (GB pilot, ~200 ms each).

The structural tests always run. The explain() tests need a real mongod (mongomock has no query planner):
    WORKER_TEST_MONGO_URI=mongodb://127.0.0.1:27099 pytest tests/test_query_plans.py
They use a throwaway database and refuse MongoDB Atlas.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import mongomock
import pytest
from bson import ObjectId

from app import jobs as q
from app import leads
from app.osm.mapping import element_to_lead
from app.overture import mapping as ovm
from tests.conftest import OWNER
from tests.test_batch_writes import MIX, row

LEAD_TS = Path(__file__).resolve().parents[2] / "web" / "src" / "models" / "Lead.ts"


def overture_lead(gid="gx", name="Iron Fist", phone="+44 20 7946 0001"):
    return ovm.row_to_lead(row(gid, name, 51.5, -0.1, phone=phone, website="https://ironfist.test"),
                           release="2026-10-21.0", now=q.now())


def osm_lead(n=1):
    el = {"type": "node", "id": n, "lat": 51.5, "lon": -0.1, "tags": {"name": "Iron Fist", "sport": "boxing",
                                                                    "phone": "+44 20 7946 0001"}}
    return element_to_lead(el, now=q.now(), area_country_code="GB")


def violations(filt, path="") -> list[str]:
    """Every equality on a partial-index field must carry $type: "string"; a top-level $or must repeat ownerId."""
    out = []
    if isinstance(filt, dict):
        for k, v in filt.items():
            if k in leads.PARTIAL_FIELDS and not (isinstance(v, dict) and v.get("$type") == "string"):
                out.append(f"{path}{k} without $type")
            if k == "$or" and path == "" and "ownerId" not in filt:
                out += [f"$or[{i}] without ownerId" for i, b in enumerate(v) if "ownerId" not in b]
            out += violations(v, f"{path}{k}.")
    elif isinstance(filt, list):
        for i, v in enumerate(filt):
            out += violations(v, f"{path}{i}.")
    return out


def test_partial_fields_match_the_web_index_definitions():
    if not LEAD_TS.exists():
        pytest.skip("web/ not present (e.g. inside the worker Docker image)")
    text = LEAD_TS.read_text(encoding="utf-8")
    found = set(re.findall(r'partialFilterExpression:\s*\{\s*"?([\w.]+)"?\s*:\s*\{\s*\$type:\s*"string"', text))
    assert found == set(leads.PARTIAL_FIELDS)


def test_filter_builders_can_use_the_partial_indexes():
    assert violations(leads._id_filter(OWNER, "node/1")) == []
    assert violations(leads._overture_filter(OWNER, overture_lead())) == []
    items = [ovm.row_to_lead(r, release="2026-10-21.0", now=q.now()) for r in MIX]
    assert violations(leads._batch_filter(OWNER, items)) == []
    assert violations({"ownerId": OWNER, "externalIds.osm": "node/1"}) == ["externalIds.osm without $type"]


class Recording:
    def __init__(self, coll):
        self.coll, self.filters = coll, []

    def __getattr__(self, name):
        attr = getattr(self.coll, name)
        if name in ("find", "find_one", "count_documents"):
            def wrapped(filt=None, *a, **kw):
                self.filters.append(filt or {})
                return attr(filt, *a, **kw)
            return wrapped
        return attr


def test_every_lookup_the_worker_sends_is_index_friendly():
    db = mongomock.MongoClient(tz_aware=True).t
    for field in leads.PARTIAL_FIELDS:
        db.leads.create_index([("ownerId", 1), (field, 1)], unique=True,
                              partialFilterExpression={field: {"$type": "string"}})
    rec = Recording(db.leads)
    for n in (1, 2, 1):
        leads.upsert_lead(rec, owner_id=OWNER, job_id="j", lead=osm_lead(n), now=q.now())
    for gid in ("g1", "g2", "g1"):
        leads.upsert_overture_lead(rec, owner_id=OWNER, job_id="j", lead=overture_lead(gid), now=q.now())
    leads.upsert_overture_batch(rec, owner_id=OWNER, job_id="j",
                                items=[ovm.row_to_lead(r, release="2026-10-21.0", now=q.now()) for r in MIX],
                                now=q.now())
    assert rec.filters and all(violations(f) == [] for f in rec.filters), [violations(f) for f in rec.filters]


# ---------------------------------------------------------------- real planner (optional)

URI = os.getenv("WORKER_TEST_MONGO_URI", "")
live = pytest.mark.skipif(not URI, reason="set WORKER_TEST_MONGO_URI to a throwaway local mongod")


@pytest.fixture
def real():
    import pymongo
    assert "mongodb.net" not in URI, "never run against MongoDB Atlas"
    client = pymongo.MongoClient(URI, tz_aware=True, serverSelectionTimeoutMS=5000)
    name = f"worker_plans_{uuid.uuid4().hex[:8]}"
    db = client[name]
    A = pymongo.ASCENDING
    for field in leads.PARTIAL_FIELDS:  # the same indexes as web/src/models/Lead.ts
        db.leads.create_index([("ownerId", A), (field, A)], unique=True,
                              partialFilterExpression={field: {"$type": "string"}})
    for field in ("externalIds.osmAliases", "externalIds.overtureAliases", "externalIds.overtureSources",
                  "website.domain", "phones.e164", "nameNormalized"):
        db.leads.create_index([("ownerId", A), (field, A)])
    db.leads.create_index([("ownerId", A), ("dataLane", A), ("discoveredAt", -1)])
    db.leads.create_index([("ownerId", A), ("saved", A), ("updatedAt", -1)])
    docs = []
    for i in range(3000):  # one user with 3,000 leads in both lanes, like a big account
        lane = {"dataLane": "overture", "externalIds": {"overture": f"g{i}", "overtureSources": [f"meta:{i}"]},
                "dedupeKey": f"overture|gym {i}|51.000|-1.000"} if i % 2 else {
            "externalIds": {"osm": f"node/{i}"}, "dedupeKey": f"gym {i}|51.000|-1.000"}
        docs.append({"ownerId": OWNER, "name": f"Gym {i}", "nameNormalized": f"gym {i}", "saved": False,
                     "website": {"domain": f"gym{i}.test"}, "phones": [{"e164": f"+4420794{i:05d}"}],
                     "updatedAt": q.now(), "discoveredAt": q.now(), **lane})
    db.leads.insert_many(docs)
    yield db
    client.drop_database(name)


def examined(db, filt) -> int:
    return db.leads.find(filt).limit(1).explain()["executionStats"]["totalDocsExamined"]


@live
def test_lookups_for_new_leads_examine_almost_nothing(real):
    new_osm = osm_lead(999_999)
    new_ov = overture_lead("not-saved-yet")
    assert examined(real, leads._id_filter(OWNER, "node/999999")) <= 2
    assert examined(real, leads._overture_filter(OWNER, new_ov)) <= 2
    assert examined(real, {"ownerId": OWNER, "dedupeKey": leads.str_eq("overture|nope|1|1")}) <= 2
    items = [ovm.row_to_lead(r, release="2026-10-21.0", now=q.now()) for r in MIX]
    assert examined(real, leads._batch_filter(OWNER, items)) <= 2
    # the same-business candidate query, both lanes
    for lead in (new_osm, new_ov):
        ors = [{"nameNormalized": lead["nameNormalized"]}, {"phones.e164": {"$in": ["+449999999999"]}}]
        assert examined(real, {"ownerId": OWNER, **leads.lane_filter(leads.lane_of(lead)), "$or": ors}) <= 2


@live
def test_the_old_query_shape_really_scanned_everything(real):
    """Proves the test above can fail: the pre-fix filter examines every lead of the user."""
    old = {"ownerId": OWNER, "dataLane": "overture", "$or": [{"externalIds.overture": "nope"},
                                                            {"externalIds.overtureAliases": "nope"}]}
    assert examined(real, old) >= 1000
    assert examined(real, {"ownerId": OWNER, "externalIds.osm": "node/nope"}) >= 1000


@live
def test_batch_upsert_on_a_real_server(real):
    other = ObjectId()
    items = [ovm.row_to_lead(r, release="2026-10-21.0", now=q.now()) for r in MIX]
    res = leads.upsert_overture_batch(real.leads, owner_id=other, job_id="j", items=items, now=q.now())
    assert res.count("new") == 7 and real.leads.count_documents({"ownerId": other}) == 7
    items = [ovm.row_to_lead(r, release="2026-10-21.0", now=q.now()) for r in MIX]
    assert set(leads.upsert_overture_batch(real.leads, owner_id=other, job_id="j2", items=items,
                                           now=q.now())) == {"updated"}
    assert real.leads.count_documents({"ownerId": other}) == 7
