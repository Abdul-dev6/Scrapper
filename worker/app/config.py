import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # worker/.env (or the current directory)
# Fallback: the repository root .env. override=False, so worker/.env and real env vars always win.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    mongodb_uri: str
    mongodb_db: str
    worker_id: str
    poll_interval: float
    stale_after: int
    max_attempts: int
    crawler_contact_url: str
    # Phase 2b: OpenStreetMap from Geofabrik extracts (public Overpass is not used at all).
    osm_cache_dir: str = "/data/osm"  # persistent Railway Volume: filtered gym files + cached index
    osm_tmp_dir: str = ""  # container disk for original downloads ("" = <system temp>/leadfinder-osm)
    osm_cache_max_bytes: int = 4 * 1024**3  # budget for filtered files (Railway Hobby volume max 5 GB)
    osm_volume_margin_bytes: int = 256 * 1024**2  # always leave this much free on the volume
    osm_tmp_margin_bytes: int = 1024**3  # ...and on the temporary disk
    osm_refresh_days: float = 7
    osm_max_download_bytes: int = 6 * 1024**3  # one extract
    osm_max_job_download_bytes: int = 8 * 1024**3  # everything one job may download
    geofabrik_index_url: str = "https://download.geofabrik.de/index-v1.json"
    crawl_concurrency: int = 10  # websites crawled at the same time (each site still gets 1 request / delay)
    crawl_delay: float = 2.5  # seconds between two requests to the same website
    crawl_max_pages: int = 5  # pages per website: home + up to 4 contact/about pages
    crawl_timeout: float = 15  # seconds to wait for one page
    crawl_recheck_days: int = 7  # a website checked more recently than this is not crawled again
    # Step 6: Google Places API (New). Empty key = Google is switched off, jobs run OSM-only.
    google_api_key: str = ""
    google_places_url: str = "https://places.googleapis.com/v1"
    # Internal monthly limits, kept BELOW Google's free caps (Text Search Pro 5,000; Place Details Essentials 10,000).
    google_text_search_monthly_limit: int = 4500
    google_details_monthly_limit: int = 9000
    google_text_search_free_cap: int = 5000
    google_details_free_cap: int = 10000
    # Per-job caps ("per city search")
    google_max_text_search_per_job: int = 50
    google_max_details_per_job: int = 1000
    # Google's free usage is counted per calendar month in Pacific time.
    google_usage_timezone: str = "America/Los_Angeles"

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_api_key)


def load_settings() -> Settings:
    uri = os.getenv("MONGODB_URI", "")
    if not uri.startswith("mongodb"):
        raise RuntimeError("MONGODB_URI is missing or invalid. Copy .env.example to .env and fill it in.")
    d = Settings("", "", "", 0, 0, 0, "")

    def num(name: str, default, cast=int):
        return cast(os.getenv(name, str(default)))

    return Settings(
        mongodb_uri=uri,
        mongodb_db=os.getenv("MONGODB_DB", "leadscraper"),
        worker_id=os.getenv("WORKER_ID", "worker-1"),
        poll_interval=float(os.getenv("POLL_INTERVAL_SECONDS", "3")),
        stale_after=int(os.getenv("STALE_AFTER_SECONDS", "600")),
        max_attempts=int(os.getenv("MAX_ATTEMPTS", "3")),
        crawler_contact_url=os.getenv("CRAWLER_CONTACT_URL", "").strip(),
        osm_cache_dir=os.getenv("OSM_CACHE_DIR", d.osm_cache_dir),
        osm_tmp_dir=os.getenv("OSM_TMP_DIR", d.osm_tmp_dir),
        osm_cache_max_bytes=num("OSM_CACHE_MAX_BYTES", d.osm_cache_max_bytes),
        osm_volume_margin_bytes=num("OSM_VOLUME_MARGIN_BYTES", d.osm_volume_margin_bytes),
        osm_tmp_margin_bytes=num("OSM_TMP_MARGIN_BYTES", d.osm_tmp_margin_bytes),
        osm_refresh_days=num("OSM_REFRESH_DAYS", d.osm_refresh_days, float),
        osm_max_download_bytes=num("OSM_MAX_DOWNLOAD_BYTES", d.osm_max_download_bytes),
        osm_max_job_download_bytes=num("OSM_MAX_JOB_DOWNLOAD_BYTES", d.osm_max_job_download_bytes),
        geofabrik_index_url=os.getenv("GEOFABRIK_INDEX_URL", d.geofabrik_index_url),
        crawl_concurrency=num("CRAWL_CONCURRENCY", d.crawl_concurrency),
        crawl_delay=max(2.0, num("CRAWL_DELAY_SECONDS", d.crawl_delay, float)),  # never ruder than 2 s
        crawl_max_pages=min(5, num("CRAWL_MAX_PAGES", d.crawl_max_pages)),  # rule: max 5
        crawl_timeout=num("CRAWL_TIMEOUT_SECONDS", d.crawl_timeout, float),
        crawl_recheck_days=num("CRAWL_RECHECK_DAYS", d.crawl_recheck_days),
        google_api_key=os.getenv("GOOGLE_PLACES_API_KEY", "").strip(),
        # Never above Google's free cap: a typo in .env must not turn into a bill.
        google_text_search_monthly_limit=min(d.google_text_search_free_cap, num(
            "GOOGLE_TEXT_SEARCH_MONTHLY_LIMIT", d.google_text_search_monthly_limit)),
        google_details_monthly_limit=min(d.google_details_free_cap, num(
            "GOOGLE_PLACE_DETAILS_MONTHLY_LIMIT", d.google_details_monthly_limit)),
        # Per-job caps can be lowered, never raised above the agreed maximums.
        google_max_text_search_per_job=min(50, num("GOOGLE_MAX_TEXT_SEARCH_PER_JOB", d.google_max_text_search_per_job)),
        google_max_details_per_job=min(1000, num("GOOGLE_MAX_DETAILS_PER_JOB", d.google_max_details_per_job)),
        google_usage_timezone=os.getenv("GOOGLE_USAGE_TIMEZONE", d.google_usage_timezone),
    )
