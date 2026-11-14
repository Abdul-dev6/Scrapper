"""GeoNames -> the `gazetteer` collection, which the web app searches to let users pick a place.

    python -m app.geo.gazetteer load <dir with cities1000.zip, countryInfo.txt, admin1CodesASCII.txt>

Documents (shared by all users; public data, no user data):
  country:GB       kind=country   name, countryCode, keys (names/codes), population, geonameId
  admin1:GB.ENG    kind=admin1    name, countryCode, admin1Code, cityBbox (from its towns), population
  gn:2643743       kind=city      name, countryCode, admin1Code/admin1Name, lat, lon, population
`keys`    normalized names of the place itself (search hits these)
`ctxKeys` normalized names/codes of its country and region ("london, uk" filters on these)

Loading is atomic: the new data goes into `gazetteer_new`, then replaces `gazetteer` in one rename,
so searches never see a half-loaded list. A `gazetteerMeta` document records the source files'
sha256, sizes and counts (dataset version).
GeoNames is licensed CC BY 4.0: the web app shows "Place names: GeoNames (CC BY 4.0)".
"""

from __future__ import annotations

import csv
import io
import sys
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path

from pymongo import ASCENDING, DESCENDING, InsertOne
from pymongo.database import Database

from .data import sha256_file
from .text import norm

csv.field_size_limit(10_000_000)

MAX_KEYS = 40
MAX_KEY_LEN = 60
BATCH = 2000
# Common ways people write a country that aren't GeoNames names/codes. Configuration, not data.
COUNTRY_ALIASES = {
    "GB": ["uk", "u k", "britain", "great britain", "united kingdom of great britain and northern ireland"],
    "US": ["usa", "u s a", "u s", "america", "united states of america"],
    "AE": ["uae", "u a e"],
    "KR": ["south korea"],
    "KP": ["north korea"],
    "RU": ["russia"],
    "CZ": ["czech republic"],
    "NL": ["holland"],
}


def _keys(*names: Iterable[str] | str) -> list[str]:
    out: list[str] = []
    for group in names:
        for n in [group] if isinstance(group, str) else group:
            k = norm(n)
            if k and len(k) <= MAX_KEY_LEN and k not in out:
                out.append(k)
            if len(out) >= MAX_KEYS:
                return out
    return out


def read_countries(path: Path) -> dict[str, dict]:
    """countryInfo.txt: #ISO ISO3 ISO-Numeric fips Country Capital Area Population Continent tld ... geonameid ..."""
    out = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            c = line.rstrip("\n").split("\t")
            iso, iso3, name, population, geoname_id = c[0], c[1], c[4], c[7], c[16]
            out[iso] = {
                "_id": f"country:{iso}", "kind": "country", "name": name, "countryCode": iso, "countryName": name,
                "keys": _keys(name, iso, iso3, COUNTRY_ALIASES.get(iso, [])),
                "population": int(population or 0), "geonameId": int(geoname_id) if geoname_id else None,
            }
    return out


def read_admin1(path: Path) -> dict[str, dict]:
    """admin1CodesASCII.txt: code (GB.ENG), name, name ascii, geonameid."""
    out = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            c = line.rstrip("\n").split("\t")
            if len(c) < 4:
                continue
            code, name, ascii_name, geoname_id = c[:4]
            cc, _, a1 = code.partition(".")
            out[code] = {
                "_id": f"admin1:{code}", "kind": "admin1", "name": name, "countryCode": cc, "admin1Code": a1,
                "keys": _keys(name, ascii_name), "population": 0,
                "geonameId": int(geoname_id) if geoname_id.isdigit() else None,
            }
    return out


def iter_cities(zip_path: Path) -> Iterator[list[str]]:
    """The 19 tab-separated `geoname` columns (see GeoNames readme.txt)."""
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist() if n.endswith(".txt"))
        with z.open(name) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8"), delimiter="\t", quoting=csv.QUOTE_NONE)
            for row in reader:
                if len(row) >= 19:
                    yield row


def build_docs(src: Path) -> tuple[list[dict], dict]:
    countries = read_countries(src / "countryInfo.txt")
    admin1 = read_admin1(src / "admin1CodesASCII.txt")
    for doc in countries.values():
        doc["ctxKeys"] = []
    for code, doc in admin1.items():
        country = countries.get(doc["countryCode"])
        doc["countryName"] = country["name"] if country else doc["countryCode"]
        doc["ctxKeys"] = country["keys"] if country else [norm(doc["countryCode"])]

    cities: list[dict] = []
    extent: dict[str, list[float]] = {}
    pop: dict[str, int] = defaultdict(int)
    for c in iter_cities(src / "cities1000.zip"):
        geoname_id, name, ascii_name, alternates = c[0], c[1], c[2], c[3]
        lat, lon, cc, a1, population = float(c[4]), float(c[5]), c[8], c[10], int(c[14] or 0)
        a1_doc = admin1.get(f"{cc}.{a1}")
        country = countries.get(cc)
        cities.append({
            "_id": f"gn:{geoname_id}", "kind": "city", "name": name, "countryCode": cc,
            "countryName": country["name"] if country else cc,
            "admin1Code": a1, "admin1Name": a1_doc["name"] if a1_doc else "",
            "lat": lat, "lon": lon, "population": population, "geonameId": int(geoname_id), "featureCode": c[7],
            "keys": _keys(name, ascii_name, [a for a in alternates.split(",") if a and not a.startswith("http")]),
            "ctxKeys": (country["keys"] if country else [norm(cc)]) + (a1_doc["keys"] if a1_doc else []),
        })
        if a1_doc:  # the region's extent from its towns: a fallback area when no region shape matches
            key = f"{cc}.{a1}"
            e = extent.setdefault(key, [lat, lon, lat, lon])
            e[0], e[1], e[2], e[3] = min(e[0], lat), min(e[1], lon), max(e[2], lat), max(e[3], lon)
            pop[key] += population
    for key, doc in admin1.items():
        if key in extent:
            doc["cityBbox"] = extent[key]  # [south, west, north, east]
        doc["population"] = pop.get(key, 0)
    docs = [*countries.values(), *admin1.values(), *cities]
    meta = {
        "_id": "gazetteer", "source": "GeoNames (https://www.geonames.org)", "license": "CC BY 4.0",
        "files": {p.name: {"sha256": sha256_file(p), "bytes": p.stat().st_size}
                  for p in (src / "cities1000.zip", src / "countryInfo.txt", src / "admin1CodesASCII.txt")},
        "counts": {"country": len(countries), "admin1": len(admin1), "city": len(cities)},
        "loadedAt": datetime.now(timezone.utc),
    }
    return docs, meta


def load(db: Database, src: Path) -> dict:
    docs, meta = build_docs(src)
    tmp = db["gazetteer_new"]
    tmp.drop()
    for i in range(0, len(docs), BATCH):
        tmp.bulk_write([InsertOne(d) for d in docs[i:i + BATCH]], ordered=False)
    tmp.create_index([("keys", ASCENDING), ("population", DESCENDING)], name="keys_1_population_-1")
    tmp.create_index([("kind", ASCENDING), ("countryCode", ASCENDING)], name="kind_1_countryCode_1")
    if tmp.count_documents({}) != len(docs):
        raise RuntimeError("Gazetteer load incomplete; the live collection was not replaced.")
    tmp.rename("gazetteer", dropTarget=True)  # atomic swap
    db.gazetteerMeta.replace_one({"_id": "gazetteer"}, meta, upsert=True)
    return meta


def main(argv: list[str]) -> None:
    if len(argv) != 3 or argv[1] != "load":
        print("Usage: python -m app.geo.gazetteer load <folder with cities1000.zip, countryInfo.txt, admin1CodesASCII.txt>")
        raise SystemExit(2)
    from ..config import load_settings
    from ..db import connect
    client, db = connect(load_settings())
    try:
        meta = load(db, Path(argv[2]))
        print(f"Loaded gazetteer into '{db.name}': {meta['counts']}")
    finally:
        client.close()


if __name__ == "__main__":
    main(sys.argv)
