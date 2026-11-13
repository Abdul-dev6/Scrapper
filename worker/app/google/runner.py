"""Run Google discovery next to OpenStreetMap discovery (in a thread), then record place IDs.

Google results are NEVER matched with leads (decision 2026-11-14); see merge.py.

Modes reported on the job (job.google.mode):
  off             the user didn't ask for Google
  osm-only        Google asked for, but unavailable: no key / monthly limit reached / API error
  google+osm      both providers searched
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime

import httpx
from pymongo.database import Database

from ..config import Settings
from . import discovery, merge
from .places import PlacesClient
from .usage import DETAILS, TEXT_SEARCH, UsageLimiter


def make_usage(db: Database, settings: Settings, clock=None) -> UsageLimiter:
    kw = {"clock": clock} if clock else {}
    return UsageLimiter(
        db,
        limits={TEXT_SEARCH: settings.google_text_search_monthly_limit, DETAILS: settings.google_details_monthly_limit},
        free_caps={TEXT_SEARCH: settings.google_text_search_free_cap, DETAILS: settings.google_details_free_cap},
        tz=settings.google_usage_timezone,
        **kw,
    )


class GoogleRun:
    """Lifecycle: start() right after geocoding, finish() after OSM leads are saved."""

    def __init__(self, db: Database, job: dict, settings: Settings, *, usage: UsageLimiter,
                 transport: httpx.BaseTransport | None = None, sleep=None):
        self.db, self.job, self.settings, self.usage = db, job, settings, usage
        self.transport = transport
        self.sleep = sleep
        self.stop = threading.Event()
        self.mode = "off"
        self.note = ""
        self._client: httpx.Client | None = None
        self._places: PlacesClient | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._future: Future | None = None

    def wanted(self) -> bool:
        # A job made by the Retry button never uses Google, even if its sources say so (the web app
        # strips "google" on retry; this is the second line of defense). Google needs a new search.
        if self.job.get("retryOf"):
            return False
        return "google" in (self.job.get("sources") or [])

    def start(self, bbox: list[float], now: datetime) -> None:
        if not self.wanted():
            return
        if not self.settings.google_enabled:
            self.mode, self.note = "osm-only", "Google is not configured on the worker (GOOGLE_PLACES_API_KEY)."
            return
        if not self.usage.available():
            st = self.usage.status()
            self.mode = "osm-only"
            self.note = f"Monthly Google limit reached. Google resumes on {st['resetsAt']:%Y-%m-%d}."
            return
        self.mode = "google+osm"
        self._client = httpx.Client(transport=self.transport, timeout=httpx.Timeout(20.0, connect=10.0))
        kw = {"sleep": self.sleep} if self.sleep else {}
        self._places = PlacesClient(self._client, api_key=self.settings.google_api_key,
                                    base_url=self.settings.google_places_url, usage=self.usage,
                                    job_id=self.job["_id"], **kw)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="google")
        self._future = self._pool.submit(
            discovery.discover, self._places, self.db, owner_id=self.job["ownerId"],
            categories=self.job["categories"], bbox=bbox,
            max_requests=self.settings.google_max_text_search_per_job, now=now, stop=self.stop,
        )

    def wait(self, heartbeat, poll: float = 5.0) -> discovery.GoogleDiscovery | None:
        """Wait for the thread, calling heartbeat() (which raises on cancel) every few seconds."""
        if self._future is None:
            return None
        while True:
            try:
                return self._future.result(timeout=poll)
            except TimeoutError:
                heartbeat()

    def finish(self, *, bbox, now: datetime, heartbeat) -> dict:
        """Record the place IDs Google found (no matching with leads) and return the job's report."""
        report: dict = {"mode": self.mode, "note": self.note}
        if self._future is None:
            return report
        try:
            found = self.wait(heartbeat)
        except Exception as exc:  # a bug in the Google part must not lose the OSM results
            report.update(mode="osm-only", note=f"Google search failed: {type(exc).__name__}")
            return report
        assert found is not None and self._places is not None
        refs = merge.record_refs(self.db, owner_id=self.job["ownerId"], job_id=self.job["_id"],
                                 candidates=found.candidates, area_query=self.job["location"]["query"], now=now)
        if found.stopped == "quota":
            report["mode"] = "osm-only"
            report["note"] = ("Monthly Google limit reached during this search; the rest used OpenStreetMap only. "
                              f"Google resumes on {self.usage.status()['resetsAt']:%Y-%m-%d}.")
        elif found.stopped == "error" or (found.error and not found.candidates):
            report["note"] = found.error or "Google Places stopped with an error."
            if not found.candidates:
                report["mode"] = "osm-only"
        elif found.stopped == "budget":
            report["note"] = (f"Used this search's Google budget ({self.settings.google_max_text_search_per_job} "
                              "searches). Run it again to search deeper.")
        report.update(
            textSearchRequests=self._places.requests[TEXT_SEARCH],
            placesFound=len(found.candidates),
            newPlaces=refs.new,
            tilesSearched=found.tiles_searched,
            tilesSkipped=found.tiles_skipped,
        )
        return report

    def close(self) -> None:
        self.stop.set()
        if self._pool:
            self._pool.shutdown(wait=True, cancel_futures=True)
        if self._client:
            self._client.close()
