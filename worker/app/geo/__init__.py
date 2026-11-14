"""Offline geocoding (Phase 2a). Replaces public Nominatim (decision 2026-11-14).

  - Place search (web): GeoNames (CC BY 4.0) cities/regions/countries loaded into the `gazetteer`
    collection by `python -m app.geo.gazetteer load` (see gazetteer.py).
  - Search areas (worker): Natural Earth (public domain) country/region shapes and urban-area outlines,
    pinned by version + sha256 (see data.py, boundaries.py).
No OSM/ODbL data is used here, so the same areas are valid for the OSM and the non-OSM lane.
"""
