import httpx
import pytest

from app.categories import CATEGORY_OSM_FILTERS
from app.osm import overpass
from app.osm.ratelimit import RateLimiter
from tests.conftest import OVERPASS, FakeOSM


def test_every_category_selector_is_understood_by_the_local_matcher():
    for selectors in CATEGORY_OSM_FILTERS.values():
        for s in selectors:
            assert overpass.parse_selector(s)


def test_match_categories():
    assert overpass.match_categories({"sport": "boxing;kickboxing"}) == ["boxing_club", "martial_arts_school"]
    assert set(overpass.match_categories({"leisure": "fitness_centre"})) == {"gym", "fitness_center"}
    assert overpass.match_categories({"sport": "kickboxing"}) == ["martial_arts_school"]  # not boxing_club
    assert overpass.match_categories({"shop": "sports"}) == ["sports_store"]
    assert overpass.match_categories({"amenity": "cafe"}) == []


def test_build_query_with_area_dedupes_shared_selectors_and_requires_name():
    query = overpass.build_query(["gym", "fitness_center"], area_id=3600065606, timeout=60)
    assert "[out:json][timeout:60];" in query
    assert "area(id:3600065606)->.searchArea;" in query
    assert query.count('nwr["leisure"="fitness_centre"]["name"](area.searchArea);') == 1
    assert query.strip().endswith("out center tags;")


def test_build_query_falls_back_to_bbox():
    query = overpass.build_query(["boxing_club"], bbox=[51.2, -0.5, 51.7, 0.3])
    assert '["name"](51.2,-0.5,51.7,0.3);' in query
    with pytest.raises(ValueError):
        overpass.build_query(["boxing_club"])


def test_build_query_combines_area_and_tile():
    # "inside the country AND inside this piece", so border tiles don't leak into neighbours
    query = overpass.build_query(["boxing_club"], area_id=3600062149, bbox=[50, -5, 52, 0])
    assert '["name"](area.searchArea)(50,-5,52,0);' in query


def run(fake):
    return overpass.run_query("q", client=fake.client(), url=OVERPASS)


def test_run_query_returns_elements():
    assert run(FakeOSM(elements=[{"id": 1}])) == [{"id": 1}]


@pytest.mark.parametrize("status", [429, 502, 503])
def test_busy_statuses(status):
    with pytest.raises(overpass.OverpassBusy):
        run(FakeOSM(overpass_status=status))


def test_too_heavy_signals():
    with pytest.raises(overpass.OverpassTooHeavy):
        run(FakeOSM(overpass_status=504))
    timeout = {"elements": [], "remark": "runtime error: Query timed out in \"query\" at line 3 after 61 seconds."}
    with pytest.raises(overpass.OverpassTooHeavy):
        run(FakeOSM(overpass_responses=[httpx.Response(200, json=timeout)]))
    memory = {"elements": [], "remark": "runtime error: Query run out of memory using about 2048 MB of RAM."}
    with pytest.raises(overpass.OverpassTooHeavy):
        run(FakeOSM(overpass_responses=[httpx.Response(200, json=memory)]))


def test_dropped_connection_is_too_heavy():
    def drop(_request):
        raise httpx.ReadError("connection reset")

    client = httpx.Client(transport=httpx.MockTransport(drop))
    with pytest.raises(overpass.OverpassTooHeavy):
        overpass.run_query("q", client=client, url=OVERPASS)


def test_bad_request_is_a_plain_error():
    with pytest.raises(overpass.OverpassError, match="HTTP 400") as info:
        run(FakeOSM(overpass_status=400))
    assert not isinstance(info.value, (overpass.OverpassBusy, overpass.OverpassTooHeavy))


STATUS = "https://overpass.test/api/status"


def test_wait_for_slot_waits_only_as_long_as_needed():
    slept = []
    busy = (
        "Rate limit: 2\n"
        "Slot available after: 2026-11-11T02:00:10Z, in 12 seconds.\n"
        "Slot available after: 2026-11-11T02:00:40Z, in 42 seconds.\n"
    )
    assert overpass.wait_for_slot(FakeOSM(status_text=busy).client(), STATUS, slept.append) == 13
    assert slept == [13]  # the soonest slot, +1 s

    slept.clear()
    assert overpass.wait_for_slot(FakeOSM().client(), STATUS, slept.append) == 0  # "2 slots available now"
    assert slept == []


def test_rate_limiter_spaces_out_calls():
    t = [100.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        t[0] += seconds

    limiter = RateLimiter(1.1, clock=lambda: t[0], sleep=sleep)
    limiter.wait()
    limiter.wait()
    t[0] += 5
    limiter.wait()
    assert slept == [pytest.approx(1.1)]


def test_cant_connect_is_busy_not_too_heavy():
    # Seen live: the server was down, connections timed out, and the area was split for nothing.
    def no_answer(_request):
        raise httpx.ConnectTimeout("timed out")

    client = httpx.Client(transport=httpx.MockTransport(no_answer))
    with pytest.raises(overpass.OverpassBusy):
        overpass.run_query("q", client=client, url=OVERPASS)
