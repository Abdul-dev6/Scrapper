"""Overture Maps Places (Phase 2c): a separate, non-OSM data lane (licensing mode M2).

Overture records are never matched, merged or linked with OpenStreetMap records; deduplication
happens only between Overture records. Licensing: Overture places are CDLA-Permissive-2.0 or
Apache-2.0 depending on the upstream source (each record's `sources[].license` is kept);
attribution "Overture Maps Foundation, overturemaps.org" plus the per-source datasets.
"""
