"""The REAL pyosmium filter against small generated .osm.pbf files.

pyosmium's native library is blocked by Windows Application Control on the development PC, so these
tests run in the production Docker image (python:3.12 + osmium 4.3.2). Elsewhere they are skipped
with an explicit reason - never silently.
"""

import pytest

try:
    import osmium
    from osmium.osm.mutable import Node, Relation, Way
    HAVE_OSMIUM = True
except Exception as exc:  # ImportError, or a blocked DLL (OSError/ImportError)
    HAVE_OSMIUM = False
    WHY = f"pyosmium can't load here ({type(exc).__name__}); run in the worker Docker image"

pytestmark = [pytest.mark.real_osmium,
              pytest.mark.skipif(not HAVE_OSMIUM, reason=WHY if not HAVE_OSMIUM else "")]

from app import jobs as q  # noqa: E402
from app.osm import extracts as ex  # noqa: E402
from app.pipeline import run_job  # noqa: E402
from tests.conftest import FakeOSM, FakeWeb, add_job  # noqa: E402


def write_pbf(path):
    """A tiny extract: named/unnamed gym nodes, a gym building (way), a gym multipolygon (relation)."""
    w = osmium.SimpleWriter(str(path))
    gym = {"leisure": "fitness_centre"}
    # tagged nodes
    w.add_node(Node(id=1, location=(-0.10, 51.50), tags={**gym, "name": "Iron Fist", "website": "https://ironfist.test"}))
    w.add_node(Node(id=2, location=(-0.11, 51.51), tags=gym))  # unnamed: not a lead
    w.add_node(Node(id=3, location=(-0.12, 51.52), tags={"amenity": "cafe", "name": "Cafe"}))  # not a gym
    w.add_node(Node(id=4, location=(-0.13, 51.53), tags={"sport": "boxing", "name": "Southpaw"}))
    # untagged nodes used by the ways
    for nid, (lon, lat) in {101: (-0.20, 51.40), 102: (-0.18, 51.40), 103: (-0.18, 51.42), 104: (-0.20, 51.42),
                            201: (0.10, 51.60), 202: (0.14, 51.60), 203: (0.14, 51.64), 204: (0.10, 51.64)}.items():
        w.add_node(Node(id=nid, location=(lon, lat)))
    w.add_way(Way(id=10, nodes=[101, 102, 103, 104, 101], tags={**gym, "name": "Big Box Gym", "building": "yes"}))
    w.add_way(Way(id=11, nodes=[101, 102], tags={"highway": "footway"}))
    w.add_way(Way(id=12, nodes=[201, 202, 203, 204, 201]))  # member of the relation, no tags
    w.add_relation(Relation(id=100, members=[("w", 12, "outer")],
                            tags={"type": "multipolygon", "sport": "martial_arts", "name": "Dojo Complex"}))
    w.close()
    return path


def test_real_filter_keeps_named_gyms_with_overpass_style_centres(tmp_path):
    pbf = write_pbf(tmp_path / "t.osm.pbf")
    beats = []
    els = {(e["type"], e["id"]): e for e in ex.filter_pbf_osmium(pbf, lambda: beats.append(1))}
    assert set(els) == {("node", 1), ("node", 4), ("way", 10), ("relation", 100)}
    assert els[("node", 1)]["lat"] == pytest.approx(51.50) and els[("node", 1)]["tags"]["website"] == "https://ironfist.test"
    assert els[("way", 10)]["center"] == {"lat": pytest.approx(51.41), "lon": pytest.approx(-0.19)}  # bbox centre
    assert els[("relation", 100)]["center"] == {"lat": pytest.approx(51.62), "lon": pytest.approx(0.12)}
    assert els[("relation", 100)]["tags"]["name"] == "Dojo Complex"
    assert beats  # heartbeats are called while reading


@pytest.mark.parametrize("damage", ["garbage", "truncated"])
def test_real_filter_rejects_a_corrupted_pbf(tmp_path, damage):
    pbf = write_pbf(tmp_path / "t.osm.pbf")
    data = pbf.read_bytes()
    pbf.write_bytes(b"not a pbf at all" * 100 if damage == "garbage" else data[: len(data) // 2])
    with pytest.raises(ex.ExtractCorrupt):
        list(ex.filter_pbf_osmium(pbf, lambda: None))


def test_end_to_end_job_with_a_real_pbf_extract(db, settings, tmp_path):
    body = write_pbf(tmp_path / "t.osm.pbf").read_bytes()
    fake = FakeOSM(bodies={"great-britain": body})
    add_job(db, categories=["gym", "boxing_club", "martial_arts_school"])
    job = q.claim_next_job(db.scrapingJobs, "w1")
    status = run_job(db, job, settings, client=fake.client(settings), sleep=lambda _s: None,
                     web_transport=FakeWeb().transport(), mx_resolve=FakeWeb().resolve)
    assert status == "completed"
    leads = {d["externalIds"]["osm"]: d for d in db.leads.find()}
    # all four are inside the test London outline [51.28..51.69, -0.51..0.33]
    assert set(leads) == {"node/1", "node/4", "way/10", "relation/100"}
    assert leads["way/10"]["sources"][0]["url"] == "https://www.openstreetmap.org/way/10"  # identity preserved
    assert leads["way/10"]["geo"]["coordinates"] == [pytest.approx(-0.19), pytest.approx(51.41)]
    assert leads["relation/100"]["categories"] == ["martial_arts_school"]
    done = db.scrapingJobs.find_one({"_id": job["_id"]})
    assert done["location"]["osmExtract"]["extracts"][0]["features"] == 4
