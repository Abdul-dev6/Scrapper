"""Offline geocoding: name normalization, the GeoNames gazetteer loader, Natural Earth search areas."""

import io
import zipfile

import httpx
import pytest

from app.geo import data, gazetteer
from app.geo.boundaries import AreaError, city_radius_km
from app.geo.text import norm
from tests.conftest import LONDON_BOX, LONDON_PLACE

# Same examples as web/tests/placeText.test.ts: both sides must normalize identically.
NORM_EXAMPLES = [
    ("London", "london"), ("São Paulo", "sao paulo"), ("Köln", "koln"), ("U.K.", "u k"),
    ("  New   York  ", "new york"), ("Saint-Étienne", "saint etienne"), ("İstanbul", "istanbul"),
    ("Straße", "straße"), ("O'Fallon", "o fallon"), ("snake_case", "snake case"), ("لاہور", "لاہور"),
    ("東京", "東京"), ("नई दिल्ली", "नई दलल"), ("", ""),
]


@pytest.mark.parametrize("raw,expected", NORM_EXAMPLES)
def test_norm(raw, expected):
    assert norm(raw) == expected


# ---------------------------------------------------------------- gazetteer loader

COUNTRY_INFO = (
    "#ISO\tISO3\tISO-Numeric\tfips\tCountry\tCapital\tArea(in sq km)\tPopulation\tContinent\ttld\tCurrencyCode\t"
    "CurrencyName\tPhone\tPostal Code Format\tPostal Code Regex\tLanguages\tgeonameid\tneighbours\tEquivalentFipsCode\n"
    "GB\tGBR\t826\tUK\tUnited Kingdom\tLondon\t244820\t66488991\tEU\t.uk\tGBP\tPound\t44\t\t\ten-GB\t2635167\tIE\t\n"
    "CA\tCAN\t124\tCA\tCanada\tOttawa\t9984670\t37058856\tNA\t.ca\tCAD\tDollar\t1\t\t\ten-CA\t6251999\tUS\t\n"
)
ADMIN1 = "GB.ENG\tEngland\tEngland\t6269131\nCA.08\tOntario\tOntario\t6093943\n"


def _city(gid, name, ascii_, alts, lat, lon, cc, a1, pop):
    return "\t".join([str(gid), name, ascii_, alts, str(lat), str(lon), "P", "PPL", cc, "", a1, "", "", "",
                      str(pop), "", "10", "Europe/London", "2024-01-01"])


def _write_geonames(folder):
    (folder / "countryInfo.txt").write_text(COUNTRY_INFO, encoding="utf-8")
    (folder / "admin1CodesASCII.txt").write_text(ADMIN1, encoding="utf-8")
    rows = [
        _city(2643743, "London", "London", "Londres,Londra,Лондон,https://en.wikipedia.org/x", 51.50853, -0.12574, "GB", "ENG", 8961989),
        _city(6058560, "London", "London", "", 42.98339, -81.23304, "CA", "08", 422324),
        _city(2643123, "Manchester", "Manchester", "", 53.48095, -2.23743, "GB", "ENG", 568996),
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cities1000.txt", "\n".join(rows) + "\n")
    (folder / "cities1000.zip").write_bytes(buf.getvalue())


def test_gazetteer_docs_have_search_and_context_keys(tmp_path):
    _write_geonames(tmp_path)
    docs, meta = gazetteer.build_docs(tmp_path)
    by_id = {d["_id"]: d for d in docs}
    assert meta["counts"] == {"country": 2, "admin1": 2, "city": 3}
    assert set(meta["files"]) == {"cities1000.zip", "countryInfo.txt", "admin1CodesASCII.txt"}  # sha256 + size

    uk = by_id["country:GB"]
    assert {"united kingdom", "gb", "gbr", "uk", "britain"} <= set(uk["keys"])
    london = by_id["gn:2643743"]
    assert london["kind"] == "city" and london["admin1Name"] == "England" and london["population"] == 8961989
    assert {"london", "londres", "лондон"} <= set(london["keys"])
    assert not any("http" in k for k in london["keys"])  # URLs in alternate names are not names
    assert {"uk", "united kingdom", "england"} <= set(london["ctxKeys"])  # "London, UK" / "London, England"
    eng = by_id["admin1:GB.ENG"]
    assert eng["cityBbox"] == [51.50853, -2.23743, 53.48095, -0.12574]  # from its towns
    assert eng["population"] == 8961989 + 568996


def test_gazetteer_load_replaces_the_collection_atomically(db, tmp_path):
    _write_geonames(tmp_path)
    db.gazetteer.insert_one({"_id": "gn:old", "kind": "city", "name": "Stale"})
    meta = gazetteer.load(db, tmp_path)
    assert db.gazetteer.count_documents({}) == 7 and db.gazetteer.find_one({"_id": "gn:old"}) is None
    assert "gazetteer_new" not in db.list_collection_names()
    assert db.gazetteerMeta.find_one({"_id": "gazetteer"})["counts"] == meta["counts"]
    hits = [d["countryCode"] for d in db.gazetteer.find({"keys": "london"}).sort("population", -1)]
    assert hits == ["GB", "CA"]  # ambiguous: both shown to the user, biggest first


# ---------------------------------------------------------------- search areas


def test_city_with_a_plausible_urban_outline(test_boundaries):
    area = test_boundaries.resolve(LONDON_PLACE)
    assert area.boundary["source"] == "naturalearth-urban-area" and area.boundary["version"] == "test-urban"
    assert [round(x, 2) for x in area.bbox] == LONDON_BOX
    assert area.contains(51.5, -0.1) and not area.contains(51.75, -0.4)
    assert area.display_name == "London, England, United Kingdom" and area.country_code == "GB"


def test_town_without_outline_gets_a_population_radius_clipped_to_its_country(test_boundaries):
    ely = {"_id": "gn:2650225", "kind": "city", "name": "Ely", "countryCode": "GB", "countryName": "United Kingdom",
           "lat": 52.39964, "lon": 0.26196, "population": 20256}
    area = test_boundaries.resolve(ely)
    assert area.boundary["source"] == "population-radius" and area.boundary["radiusKm"] == round(city_radius_km(20256), 1)
    assert area.contains(52.40, 0.26) and not area.contains(52.60, 0.26)
    coastal = {**ely, "_id": "gn:1", "lon": 1.75}  # circle crosses the (test) country edge at 1.8°E
    assert test_boundaries.resolve(coastal).bbox[3] <= 1.8 + 1e-9


def test_oversized_outline_falls_back_to_radius(test_boundaries):
    village = {**LONDON_PLACE, "_id": "gn:2", "name": "Tiny", "population": 500}  # same point, 2000 km² outline
    assert test_boundaries.resolve(village).boundary["source"] == "population-radius"


def test_region_from_natural_earth_or_from_its_towns(test_boundaries):
    wa = test_boundaries.resolve({"_id": "admin1:US.WA", "kind": "admin1", "name": "Washington", "countryCode": "US",
                                  "admin1Code": "WA", "countryName": "United States"})
    assert wa.boundary["source"] == "naturalearth-admin1" and wa.contains(47.6, -122.3)
    assert wa.boundary["matchedBy"] == "gn_a1_code"
    england = {"_id": "admin1:GB.ENG", "kind": "admin1", "name": "England", "countryCode": "GB", "admin1Code": "ENG",
               "countryName": "United Kingdom", "cityBbox": [50.0, -5.7, 55.8, 1.7]}
    eng = test_boundaries.resolve(england)
    assert eng.boundary["source"] == "naturalearth-admin1" and eng.boundary["matchedBy"] == "geonunit"
    assert eng.contains(51.5, -0.1)  # London
    assert not eng.contains(52.4, -4.0)  # Aberystwyth (Wales) is NOT in England, although it's in the towns' box
    # A region Natural Earth knows nothing about: the box around its towns, clipped to the country.
    other = test_boundaries.resolve({**england, "_id": "admin1:GB.XX", "name": "Nowhere-shire", "admin1Code": "XX",
                                     "cityBbox": [52.0, -1.0, 53.0, 0.0]})
    assert other.boundary["source"] == "geonames-region-towns-bbox" and other.contains(52.5, -0.5)


def test_country_and_errors(test_boundaries):
    gb = test_boundaries.resolve({"_id": "country:GB", "kind": "country", "name": "United Kingdom", "countryCode": "GB"})
    assert gb.boundary == {"source": "naturalearth-admin0", "id": "GB", "version": "test-admin0"}
    with pytest.raises(AreaError, match="No boundary"):
        test_boundaries.resolve({"_id": "country:ZZ", "kind": "country", "name": "Nowhere", "countryCode": "ZZ"})
    with pytest.raises(AreaError, match="pick the place"):
        test_boundaries.resolve({"_id": "x", "kind": "planet"})


def test_search_boxes_split_far_apart_parts():
    from shapely.geometry import MultiPolygon, box

    from app.geo.boundaries import Area

    geom = MultiPolygon([box(-5, 42, 8, 51), box(55.2, -21.4, 55.9, -20.8)])  # mainland + a far island
    area = Area("country", "country:FR", "France", "FR", "France", geom, {})
    boxes = area.search_boxes()
    assert len(boxes) == 2 and boxes[0] == [42, -5, 51, 8]


# ---------------------------------------------------------------- pinned data


def test_data_with_a_wrong_checksum_is_refused(tmp_path):
    ds = data.Dataset("x", "https://data.test/x.zip", "0" * 64, "1.0", "test")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"tampered")))
    with pytest.raises(data.GeoDataError, match="unexpected checksum"):
        data.ensure(ds, root=tmp_path, client=client)
    assert not (tmp_path / "raw" / "x.zip").exists()  # never kept


def test_data_with_the_right_checksum_is_downloaded_once(tmp_path):
    import hashlib

    body = b"good data"
    ds = data.Dataset("x", "https://data.test/x.zip", hashlib.sha256(body).hexdigest(), "1.0", "test")
    calls = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, content=body)))
    p1 = data.ensure(ds, root=tmp_path, client=client)
    p2 = data.ensure(ds, root=tmp_path, client=client)
    assert p1 == p2 and p1.read_bytes() == body and len(calls) == 1
    assert ds.label == f"x@1.0#{ds.sha256[:12]}"
