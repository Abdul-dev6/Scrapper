import json
import re

import httpx
import pytest

from app.osm import discovery
from app.osm.discovery import Tile, initial_tiles
from tests.conftest import OVERPASS, FakeOSM

STATUS = "https://overpass.test/api/status"
UK_BBOX = [49.0, -8.0, 61.0, 2.0]


def tile_of(query: str) -> list[float]:
    s, w, n, e = re.search(r"\(area\.searchArea\)\(([^)]+)\)", query).group(1).split(",")
    return [float(s), float(w), float(n), float(e)]


def ok(elements):
    return httpx.Response(200, content=json.dumps({"elements": elements}))


def run(fake, bbox=UK_BBOX, max_queries=100):
    sleeps = []
    pieces = list(discovery.discover(
        ["boxing_club"], area_id=3600062149, bbox=bbox, client=fake.client(), url=OVERPASS,
        status_url=STATUS, sleep=sleeps.append, timeout=60, max_queries=max_queries,
    ))
    return pieces, sleeps


def test_small_area_is_one_query():
    fake = FakeOSM(elements=[{"type": "node", "id": 1}])
    pieces, _ = run(fake, bbox=[53.3, -2.3, 53.5, -2.1])
    assert len(pieces) == 1 and pieces[0].elements == [{"type": "node", "id": 1}]
    assert pieces[0].fraction_done == pytest.approx(1.0)
    assert fake.count("overpass.test") == 1


def test_heavy_area_is_split_until_it_fits():
    # Pretend the server can only handle pieces up to 3° tall.
    def server(query):
        s, w, n, e = tile_of(query)
        if n - s > 3:
            return httpx.Response(504)
        return ok([{"type": "node", "id": int((s + 90) * 1000 + (w + 180))}])

    fake = FakeOSM(overpass_fn=server)
    pieces, _ = run(fake)
    good = [p for p in pieces if p.failed_tile is None]
    assert len(good) == 16 and all(len(p.elements) == 1 for p in good)  # 12° -> 6° -> 3°: 4 x 4 pieces
    assert fake.count("overpass.test") == 1 + 4 + 16  # whole, quarters, sixteenths
    fractions = [p.fraction_done for p in pieces]
    assert fractions == sorted(fractions) and fractions[-1] == pytest.approx(1.0)  # progress only goes up


def test_only_crowded_parts_are_split():
    # Only the south-west quarter is "crowded"; the rest is answered in one go.
    def server(query):
        s, w, n, e = tile_of(query)
        crowded = s < 55 and w < -3
        if crowded and n - s > 3:
            return httpx.Response(504)
        return ok([])

    fake = FakeOSM(overpass_fn=server)
    run(fake)
    assert fake.count("overpass.test") == 1 + 4 + 4  # whole, quarters, then only that quarter's quarters


def test_busy_server_is_retried_without_splitting():
    fake = FakeOSM(overpass_responses=[httpx.Response(429), httpx.Response(502)], elements=[{"type": "node", "id": 1}])
    pieces, sleeps = run(fake)
    assert len(pieces) == 1 and pieces[0].failed_tile is None and pieces[0].elements
    assert fake.count("overpass.test") == 3 and sum(sleeps) == 15 + 30


def test_small_piece_that_times_out_is_retried_not_split():
    # A slow, overloaded server: a small piece times out twice, then answers.
    fake = FakeOSM(overpass_responses=[httpx.Response(504), httpx.Response(504)], elements=[{"type": "node", "id": 7}])
    pieces, sleeps = run(fake, bbox=[53.9, -0.2, 54.0, -0.1])
    assert len(pieces) == 1 and pieces[0].failed_tile is None and pieces[0].elements
    assert fake.count("overpass.test") == 3 and sum(sleeps) == 15 + 30 and max(sleeps) <= 5  # paused in 5 s steps


def test_always_busy_gives_up_on_that_piece():
    pieces, _ = run(FakeOSM(overpass_status=503))
    assert len(pieces) == 1 and pieces[0].failed_tile is not None and "busy" in pieces[0].error


def test_query_cap_stops_splitting():
    pieces, _ = run(FakeOSM(overpass_status=504), max_queries=5)
    failed = [p for p in pieces if p.failed_tile is not None]
    assert failed and all("not searched" in p.error or "long" in p.error for p in failed)
    assert pieces[-1].fraction_done == pytest.approx(1.0)


def test_waits_for_a_free_slot_before_querying():
    busy = "Rate limit: 2\nSlot available after: 2026-11-11T02:00:10Z, in 7 seconds.\n"
    _, sleeps = run(FakeOSM(status_text=busy, elements=[]), bbox=[53.3, -2.3, 53.5, -2.1])
    assert sum(sleeps) == 8


def test_stops_asking_an_unreachable_status_page():
    def server(query):
        s, w, n, e = tile_of(query)
        return httpx.Response(504) if n - s > 3 else ok([])

    fake = FakeOSM(overpass_fn=server)
    fake.status_text = None  # handler below turns this into a timeout

    original = fake.handler

    def handler(request):
        if request.url.path.endswith("/status"):
            fake.calls.append(request)
            raise httpx.ReadTimeout("status page overloaded")
        return original(request)

    fake.handler = handler
    run(fake)
    status_checks = [r for r in fake.calls if r.url.path.endswith("/status")]
    assert len(status_checks) == 2  # gave up after 2 failures instead of once per piece (21 pieces)
    assert fake.count("overpass.test") == 21


def test_tiles():
    assert initial_tiles([1, 170, 2, -170]) == [Tile(1, 170, 2, 180.0), Tile(1, -180.0, 2, -170)]  # crosses 180°
    quarters = Tile(0, 0, 2, 2).split()
    assert sum(q.area for q in quarters) == pytest.approx(Tile(0, 0, 2, 2).area, rel=1e-3)
    assert not Tile(0, 0, 0.04, 0.04).can_split()


def test_cancel_is_checked_during_a_long_pause():
    checks = []
    fake = FakeOSM(overpass_responses=[httpx.Response(429), httpx.Response(429), httpx.Response(429)], elements=[])
    list(discovery.discover(
        ["boxing_club"], area_id=1, bbox=[53.3, -2.3, 53.5, -2.1], client=fake.client(), url=OVERPASS,
        status_url=STATUS, sleep=lambda _s: None, timeout=60, max_queries=100, on_wait=lambda: checks.append(1),
    ))
    # pauses of 15 + 30 + 45 s in 5 s steps = 18 steps, each followed by a check
    assert len(checks) >= 18
