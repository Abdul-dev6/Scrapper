# Step 1 image. In Step 3 we switch to a Playwright base image for JS-heavy sites.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# pyosmium (osmium wheel) needs the expat XML library at runtime; the slim image doesn't include it.
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Phase 2a: download the pinned Natural Earth boundary files at BUILD time and verify their sha256
# (app/geo/data.py). The build fails if a file doesn't match, so production never fetches them.
ENV GEO_DATA_DIR=/app/geodata
RUN python -m app.geo.data

# Phase 2b: OpenStreetMap extracts. Filtered gym files live on the persistent Railway Volume, which
# must be mounted at /data (OSM_CACHE_DIR=/data/osm). Original .osm.pbf downloads use container disk
# (/tmp) and are deleted after filtering. The worker checks free space at runtime before every step.
ENV OSM_CACHE_DIR=/data/osm OSM_TMP_DIR=/tmp/leadfinder-osm

# never run as root; the volume mount point must be writable by this user
RUN useradd --create-home --uid 10001 worker && mkdir -p /data/osm && chown -R worker:worker /data
USER worker

CMD ["python", "-m", "app.main"]
