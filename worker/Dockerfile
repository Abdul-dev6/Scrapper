# Step 1 image. In Step 3 we switch to a Playwright base image for JS-heavy sites.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# never run as root
RUN useradd --create-home --uid 10001 worker
USER worker

CMD ["python", "-m", "app.main"]
