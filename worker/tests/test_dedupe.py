"""MongoDB duplicate prevention and SSRF protection."""

import asyncio

import httpx
import pytest
from pymongo.errors import DuplicateKeyError

from app import dedupe, leads
from app import jobs as q
from app.crawl.fetcher import Fetcher, FetchError
from app.crawl.robots import Robots
from app.crawl.runner import make_async_client
from app.crawl.ssrf import ip_is_public
from app.osm.mapping import element_to_lead
from tests.conftest import OWNER, FakeWeb

JOB = "job-1"


def osm(type_, id_, name, lat, lon, **tags):
    el = {"type": type_, "id": id_, "tags": {"name": name, "sport": "boxing", **tags}}
    if type_ == "node":
        el.update(lat=lat, lon=lon)
    else:
        el["center"] = {"lat": lat, "lon": lon}
    return element_to_lead(el, now=q.now(), area_country_code="GB")


def save(db, lead):
    return leads.upsert_lead(db.leads, owner_id=OWNER, job_id=JOB, lead=lead, now=q.now())


@pytest.fixture
def idb(db):
    """The same unique indexes `npm run db:indexes` builds."""
    db.leads.create_index([("ownerId", 1), ("externalIds.osm", 1)], unique=True,
                          partialFilterExpression={"externalIds.osm": {"$type": "string"}})
    db.leads.create_index([("ownerId", 1), ("dedupeKey", 1)], unique=True,
                          partialFilterExpression={"dedupeKey": {"$type": "string"}})
    db.leads.create_index([("ownerId", 1), ("externalIds.googlePlaceId", 1)], unique=True,
                          partialFilterExpression={"externalIds.googlePlaceId": {"$type": "string"}})
    return db


# ---------------------------------------------------------------- similarity


@pytest.mark.parametrize("a,b,same", [
    ("Iron Fist", "Iron Fist Boxing Gym", True),
    ("Ironfist", "Iron Fist", True),
    ("Café Boxe", "Cafe Boxe", True),
    ("Chain Gym Leeds", "Chain Gym York", False),
    ("Iron Fist North", "Iron Fist South", False),
    ("Boxing Club", "MMA Club", False),
])
def test_name_similarity(a, b, same):
    assert (dedupe.name_similarity(a, b) >= 0.6) is same


# ---------------------------------------------------------------- OSM duplicates


def test_same_gym_mapped_as_node_and_building_is_saved_once(idb):
    node = osm("node", 1, "Iron Fist", 51.5000, -0.1000, phone="+44 20 7946 0001")
    way = osm("way", 77, "Iron Fist Boxing", 51.5003, -0.1002, phone="020 7946 0001", email="info@ironfist.test")
    assert save(idb, node) == "new"
    assert save(idb, way) == "merged"
    assert idb.leads.count_documents({}) == 1
    lead = idb.leads.find_one()
    assert lead["externalIds"]["osmAliases"] == ["way/77"]
    assert lead["name"] == "Iron Fist"  # the alias fills gaps, never overwrites
    assert [e["value"] for e in lead["emails"]] == ["info@ironfist.test"]
    assert len(lead["phones"]) == 1  # same number written two ways = one phone
    assert lead["mergeLog"][0]["reason"] == "phone"
    # Searching again: both elements land on the same lead, nothing new.
    assert save(idb, way) == "updated" and save(idb, node) == "updated"
    assert idb.leads.count_documents({}) == 1


def test_branches_stay_separate(idb):
    save(idb, osm("node", 1, "Chain Gym Leeds", 53.80, -1.55, website="https://chaingym.test/leeds"))
    save(idb, osm("node", 2, "Chain Gym York", 53.96, -1.08, website="https://chaingym.test/york"))
    save(idb, osm("node", 3, "Anytime Fitness", 51.50, -0.10))
    save(idb, osm("node", 4, "Anytime Fitness", 51.52, -0.14))  # same name, 3.5 km away
    save(idb, osm("node", 5, "Iron Fist North", 51.60, -0.10, website="https://ironfist.test"))
    save(idb, osm("node", 6, "Iron Fist South", 51.60, -0.1001, website="https://ironfist.test"))  # same building
    assert idb.leads.count_documents({}) == 6


def test_same_name_same_spot_different_osm_id_is_merged(idb):
    save(idb, osm("node", 1, "Southpaw Club", 51.5, -0.1))
    assert save(idb, osm("node", 2, "Southpaw Club", 51.5004, -0.1)) == "merged"
    assert idb.leads.count_documents({}) == 1


def test_unique_index_catches_a_race_between_two_workers(idb, monkeypatch):
    save(idb, osm("node", 1, "Southpaw Club", 51.5, -0.1))
    # Worker B checked a moment BEFORE worker A inserted, so its lookup found nothing:
    monkeypatch.setattr(leads, "find_same_business", lambda *_a, **_k: None)
    assert save(idb, osm("node", 2, "Southpaw Club", 51.5001, -0.1001)) == "merged"  # DuplicateKeyError -> update
    assert idb.leads.count_documents({}) == 1
    assert idb.leads.find_one()["externalIds"]["osmAliases"] == ["node/2"]


def test_unique_index_rejects_a_second_lead_with_the_same_place_id(idb):
    idb.leads.insert_one({"ownerId": OWNER, "name": "A", "externalIds": {"googlePlaceId": "ChIJsame00001"}})
    with pytest.raises(DuplicateKeyError):
        idb.leads.insert_one({"ownerId": OWNER, "name": "B", "externalIds": {"googlePlaceId": "ChIJsame00001"}})


def test_every_field_has_provenance(idb):
    save(idb, osm("node", 1, "Iron Fist", 51.5, -0.1, website="https://ironfist.test", phone="+44 20 7946 0001",
                  **{"addr:street": "High St", "addr:postcode": "N1 1AA"}))
    lead = idb.leads.find_one()
    for field in ("name", "address", "geo", "website"):
        assert lead["fieldSources"][field] == {"sourceUrl": "https://www.openstreetmap.org/node/1", "sourceType": "osm"}
    assert lead["phones"][0]["sourceType"] == "osm"
    assert lead["dedupeKey"].startswith("iron fist|51.500|")


# ---------------------------------------------------------------- SSRF


@pytest.mark.parametrize("ip,public", [
    ("93.184.216.34", True), ("8.8.8.8", True), ("2606:4700::1111", True),
    ("10.0.0.1", False), ("172.16.5.4", False), ("192.168.1.1", False), ("127.0.0.1", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("::1", False),
    ("fe80::1", False), ("fc00::1", False), ("::ffff:10.0.0.1", False), ("224.0.0.1", False), ("nonsense", False),
])
def test_ip_is_public(ip, public):
    assert ip_is_public(ip) is public


async def _fetch(web, url):
    async with make_async_client("LeadFinderBot/test", 5, web.transport()) as client:
        return await Fetcher(client, Robots("LeadFinderBot"), delay=0).fetch(url)


@pytest.mark.parametrize("url", [
    "http://intranet.test/",          # hostname resolving to 10.0.0.5
    "http://metadata.test/latest/",   # resolving to 169.254.169.254 (cloud metadata)
    "http://10.1.2.3/",               # private IP literal
    "http://[::1]/",                  # IPv6 loopback
    "http://localhost/",
    "http://gym.test:22/",            # SSH port
    "http://user:pass@gym.test/",     # credentials in URL
])
def test_crawler_refuses_private_targets(url):
    web = FakeWeb(pages={url: "<html>secret</html>"})
    with pytest.raises(FetchError):
        asyncio.run(_fetch(web, url))
    assert web.calls == []  # nothing was sent, not even robots.txt


def test_redirect_to_private_address_is_blocked():
    web = FakeWeb(pages={
        "https://gym.test": httpx.Response(302, headers={"location": "http://metadata.test/latest/meta-data"}),
        "http://metadata.test/latest/meta-data": "<html>credentials</html>",
    })
    with pytest.raises(FetchError, match="private"):
        asyncio.run(_fetch(web, "https://gym.test/"))
    assert not any(c.url.host == "metadata.test" for c in web.calls)


def test_public_site_still_crawled():
    web = FakeWeb(pages={"https://gym.test": "<html>ok</html>"})
    assert asyncio.run(_fetch(web, "https://gym.test/")).html == "<html>ok</html>"
