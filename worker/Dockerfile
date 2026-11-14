# Step 1 image. In Step 3 we switch to a Playwright base image for JS-heavy sites.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Phase 2a: download the pinned Natural Earth boundary files at BUILD time and verify their sha256
# (app/geo/data.py). The build fails if a file doesn't match, so production never fetches them.
ENV GEO_DATA_DIR=/app/geodata
RUN python -m app.geo.data

# never run as root
RUN useradd --create-home --uid 10001 worker
USER worker

CMD ["python", "-m", "app.main"]
