"""Bounding-box tiles (moved from osm/discovery.py). Used by the Google Text Search area subdivision."""

from __future__ import annotations

from dataclasses import dataclass

from .text import bbox_area_km2

MIN_TILE_DEG = 0.05  # ~5 km


@dataclass(frozen=True)
class Tile:
    south: float
    west: float
    north: float
    east: float

    @property
    def bbox(self) -> list[float]:
        return [self.south, self.west, self.north, self.east]

    @property
    def area(self) -> float:
        return bbox_area_km2(self.bbox)

    def can_split(self) -> bool:
        return (self.north - self.south) > MIN_TILE_DEG or (self.east - self.west) > MIN_TILE_DEG

    def split(self) -> list["Tile"]:
        lat = (self.south + self.north) / 2
        lon = (self.west + self.east) / 2
        return [
            Tile(self.south, self.west, lat, lon),
            Tile(self.south, lon, lat, self.east),
            Tile(lat, self.west, self.north, lon),
            Tile(lat, lon, self.north, self.east),
        ]

    def label(self) -> str:
        return f"{self.south:.2f},{self.west:.2f},{self.north:.2f},{self.east:.2f}"


def initial_tiles(bbox: list[float]) -> list[Tile]:
    south, west, north, east = bbox
    if west > east:  # crosses the 180° line (e.g. Fiji): two boxes, one on each side
        return [Tile(south, west, north, 180.0), Tile(south, -180.0, north, east)]
    return [Tile(south, west, north, east)]
