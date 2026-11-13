import pytest

from app import jobs as q
from app.osm import nominatim
from tests.conftest import LONDON, NOMINATIM, FakeOSM


def run(db, fake, query="London, UK", limiter=None, sleeps=None):
    from app.osm.ratelimit import RateLimiter

    return nominatim.geocode(
        query, client=fake.client(), cache=db.geocodeCache, limiter=limiter or RateLimiter(0),
        url=NOMINATIM, now=q.now(), sleep=(sleeps if sleeps is not None else []).append,
    )


def test_parses_place_and_area_id(db):
    place = run(db, FakeOSM())
    assert place["displayName"].startswith("London")
    assert place["countryCode"] == "GB"
    assert place["bbox"] == [51.28, -0.51, 51.69, 0.33]  # [south, west, north, east]
    assert place["areaId"] == 3_600_065_606
    assert place["osmUrl"] == "https://www.openstreetmap.org/relation/65606"


def test_sends_honest_query_params(db):
    fake = FakeOSM()
    run(db, fake)
    params = fake.calls[0].url.params
    assert params["q"] == "London, UK" and params["format"] == "jsonv2" and params["limit"] == "1"


def test_second_lookup_uses_cache(db):
    fake = FakeOSM()
    run(db, fake, "London, UK")
    run(db, fake, "  london,   uk ")  # same place, different spacing/case
    assert fake.count("nominatim.test") == 1


def test_node_places_have_no_area(db):
    node = {**LONDON, "osm_type": "node", "osm_id": 1, "boundingbox": ["51.4", "51.6", "-0.2", "0.0"]}
    assert run(db, FakeOSM(places=[node]))["areaId"] is None


def test_countries_are_allowed(db):
    uk = {**LONDON, "osm_type": "relation", "osm_id": 62149, "addresstype": "country",
          "display_name": "United Kingdom", "boundingbox": ["49.67", "61.06", "-14.02", "2.09"]}
    place = run(db, FakeOSM(places=[uk]), "UK")
    assert place["placeType"] == "country" and place["areaId"] == 3_600_062_149


def test_rejects_continents(db):
    europe = {**LONDON, "addresstype": "continent", "display_name": "Europe"}
    with pytest.raises(nominatim.GeocodeError, match="continent"):
        run(db, FakeOSM(places=[europe]), "Europe")


def test_unknown_place(db):
    with pytest.raises(nominatim.GeocodeError, match="Couldn't find"):
        run(db, FakeOSM(places=[]), "Atlantis")
    assert db.geocodeCache.count_documents({}) == 0  # failures aren't cached


def test_busy_location_service_is_retried(db):
    import httpx

    fake = FakeOSM()
    answers = [httpx.Response(503), httpx.Response(429)]
    real = fake.handler
    fake.handler = lambda r: (fake.calls.append(r), answers.pop(0))[1] if answers else real(r)
    sleeps = []
    place = run(db, fake, "Japan", sleeps=sleeps)
    assert place["displayName"].startswith("London")  # the fake always answers London
    assert sleeps == [5, 15] and fake.count("nominatim.test") == 3


def test_location_service_down_gives_friendly_error(db):
    import httpx

    fake = FakeOSM()
    fake.handler = lambda r: (fake.calls.append(r), httpx.Response(503))[1]
    with pytest.raises(nominatim.GeocodeError, match="HTTP 503"):
        run(db, fake)
    assert fake.count("nominatim.test") == 3  # tried 3 times, then gave up
