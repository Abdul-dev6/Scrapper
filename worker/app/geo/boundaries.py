"""The search area for a chosen place, from Natural Earth (public domain) + the gazetteer.

  country  -> Natural Earth admin-0 shape (ISO_A2_EH; ISO_A2 is "-99" for e.g. France and Norway)
  admin1   -> union of Natural Earth admin-1 shapes whose gn_a1_code is the GeoNames region code
              (e.g. US.WA); if none match (e.g. England, where NE has counties), the box around the
              region's towns from the gazetteer, clipped to the country
  city     -> the Natural Earth urban-area outline containing the town, if its size is plausible for
              the population; otherwise a circle sized by population; always clipped to the country

Every area records exactly which data produced it (`boundary`: source, id, version), so coverage and
results can be tied to a boundary version. Shapes are read on demand from the pinned zip files.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import shapefile
from shapely import affinity
from shapely.geometry import Point, box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.prepared import prep

from .data import DATASETS, GeoDataError, ensure

REGION_MARGIN_DEG = 0.05


class AreaError(RuntimeError):
    """The place can't be turned into a search area. Message is safe to show."""


@dataclass
class Area:
    kind: str
    place_id: str
    display_name: str
    country_code: str
    country: str
    geometry: BaseGeometry
    boundary: dict
    _prepared: object = field(default=None, repr=False)

    @property
    def bbox(self) -> list[float]:
        west, south, east, north = self.geometry.bounds
        return [south, west, north, east]

    def search_boxes(self, merge_deg: float = 0.25) -> list[list[float]]:
        """One [south, west, north, east] box per landmass (nearby ones merged), so a country with
        overseas parts (France: Réunion, Guiana...) isn't searched as one ocean-sized box."""
        parts = [p for p in getattr(self.geometry, "geoms", [self.geometry]) if not p.is_empty]
        clusters = unary_union([box(*p.bounds).buffer(merge_deg / 2, join_style=2) for p in parts])
        out = []
        for g in getattr(clusters, "geoms", [clusters]):
            # exact bounds of the landmasses in this cluster (the merge margin is not searched)
            w, s, e, n = unary_union([p for p in parts if g.intersects(p)]).bounds
            out.append([s, w, n, e])
        return sorted(out, key=lambda b: -(b[2] - b[0]) * (b[3] - b[1]))

    def contains(self, lat: float, lon: float) -> bool:
        if self._prepared is None:
            self._prepared = prep(self.geometry)
        return self._prepared.covers(Point(lon, lat))

    def to_checkpoint(self) -> dict:
        """What a job stores; the shape itself is re-derived from the pinned data on resume."""
        return {"kind": self.kind, "placeId": self.place_id, "displayName": self.display_name,
                "countryCode": self.country_code, "country": self.country, "bbox": self.bbox,
                "boundary": self.boundary}


def city_radius_km(population: int) -> float:
    """Search radius for a town without a usable outline: 3 km for a village, ~35 km for London."""
    return max(3.0, min(35.0, 1.5 + 3.5 * math.sqrt(max(population, 0) / 100_000)))


def _circle(lat: float, lon: float, km: float) -> BaseGeometry:
    """Approximate circle (degrees are shorter east-west away from the equator)."""
    deg_lat = km / 111.32
    deg_lon = km / (111.32 * max(math.cos(math.radians(lat)), 0.05))
    return affinity.scale(Point(lon, lat).buffer(1.0, 64), deg_lon, deg_lat)


class Boundaries:
    def __init__(self, paths: dict[str, Path], versions: dict[str, str]):
        self.paths = paths
        self.versions = versions
        self._urban_index: list[tuple[int, tuple[float, float, float, float], float]] | None = None

    @classmethod
    def from_pinned(cls, root: Path | None = None) -> "Boundaries":
        try:
            paths = {k: ensure(ds, root=root) for k, ds in DATASETS.items()}
        except GeoDataError as exc:
            raise AreaError(str(exc)) from exc
        return cls(paths, {k: ds.label for k, ds in DATASETS.items()})

    # ---------------------------------------------------------------- shapes

    def _reader(self, key: str) -> shapefile.Reader:
        return shapefile.Reader(str(self.paths[key]))

    def country(self, cc: str) -> BaseGeometry | None:
        sf = self._reader("ne_admin0")
        try:
            names = [f[0] for f in sf.fields[1:]]
            col = names.index("ISO_A2_EH")
            idx = [i for i, rec in enumerate(sf.iterRecords()) if rec[col] == cc]
            if not idx:
                return None
            return unary_union([shape(sf.shape(i).__geo_interface__) for i in idx])
        finally:
            sf.close()

    def admin1(self, cc: str, a1: str) -> BaseGeometry | None:
        sf = self._reader("ne_admin1")
        try:
            names = [f[0] for f in sf.fields[1:]]
            col = names.index("gn_a1_code")
            want = f"{cc}.{a1}"
            idx = [i for i, rec in enumerate(sf.iterRecords()) if rec[col] == want]
            if not idx:
                return None
            return unary_union([shape(sf.shape(i).__geo_interface__) for i in idx]).buffer(0)
        finally:
            sf.close()

    def urban_containing(self, lat: float, lon: float) -> tuple[BaseGeometry, float] | None:
        """(outline, area km²) of the urban area containing the point, if any."""
        sf = self._reader("ne_urban")
        try:
            if self._urban_index is None:
                names = [f[0] for f in sf.fields[1:]]
                acol = names.index("area_sqkm")
                areas = [float(r[acol] or 0) for r in sf.iterRecords()]
                self._urban_index = [(i, tuple(s.bbox), areas[i]) for i, s in enumerate(sf.iterShapes())]
            pt = Point(lon, lat)
            for i, (w, s, e, n), area in self._urban_index:
                if w <= lon <= e and s <= lat <= n:
                    geom = shape(sf.shape(i).__geo_interface__)
                    if geom.covers(pt):
                        return geom, area
            return None
        finally:
            sf.close()

    # ---------------------------------------------------------------- resolve

    def resolve(self, place: dict) -> Area:
        """place = the gazetteer document the user picked (stored on the job)."""
        kind, cc = place.get("kind"), place.get("countryCode", "")
        country_geom = self.country(cc) if cc else None
        country_name = place.get("countryName") or cc

        if kind == "country":
            if country_geom is None:
                raise AreaError(f"No boundary is available for {place.get('name', cc)}.")
            return Area(kind, place["_id"], place["name"], cc, country_name, country_geom,
                        {"source": "naturalearth-admin0", "id": cc, "version": self.versions["ne_admin0"]})

        if kind == "admin1":
            geom = self.admin1(cc, place.get("admin1Code", ""))
            boundary = {"source": "naturalearth-admin1", "id": f"{cc}.{place.get('admin1Code')}",
                        "version": self.versions["ne_admin1"]}
            if geom is None:
                b = place.get("cityBbox")
                if not b:
                    raise AreaError(f"No boundary is available for {place.get('name')}.")
                s, w, n, e = b
                m = REGION_MARGIN_DEG
                geom = box(w - m, s - m, e + m, n + m)
                boundary = {"source": "geonames-region-towns-bbox", "id": place["_id"],
                            "version": place.get("gazetteerVersion", "")}
            if country_geom is not None:
                geom = geom.intersection(country_geom)
                boundary["clippedTo"] = {"source": "naturalearth-admin0", "id": cc, "version": self.versions["ne_admin0"]}
            if geom.is_empty:
                raise AreaError(f"Couldn't build an area for {place.get('name')}.")
            name = f"{place['name']}, {country_name}"
            return Area(kind, place["_id"], name, cc, country_name, geom, boundary)

        if kind == "city":
            lat, lon, pop = float(place["lat"]), float(place["lon"]), int(place.get("population") or 0)
            radius = city_radius_km(pop)
            urban = self.urban_containing(lat, lon)
            expected_km2 = math.pi * radius * radius
            if urban and urban[1] <= expected_km2 * 4:
                geom = urban[0]
                boundary = {"source": "naturalearth-urban-area", "id": f"{lat:.5f},{lon:.5f}",
                            "version": self.versions["ne_urban"]}
            else:  # no outline, or the outline is a whole conurbation far bigger than this town
                geom = _circle(lat, lon, radius)
                boundary = {"source": "population-radius", "id": place["_id"], "radiusKm": round(radius, 1),
                            "version": "radius-v1"}
            if country_geom is not None:
                clipped = geom.intersection(country_geom)
                if not clipped.is_empty:
                    geom = clipped
                    boundary["clippedTo"] = {"source": "naturalearth-admin0", "id": cc,
                                             "version": self.versions["ne_admin0"]}
            parts = [place["name"], place.get("admin1Name") or "", country_name]
            return Area(kind, place["_id"], ", ".join(p for p in parts if p), cc, country_name, geom, boundary)

        raise AreaError("Unknown kind of place. Start a new search and pick the place from the list.")
