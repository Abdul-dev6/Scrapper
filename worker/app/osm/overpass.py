"""Overpass: find businesses matching the selected categories inside an area."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable

import httpx

from ..categories import CATEGORY_OSM_FILTERS


class OverpassError(Exception):
    """The message is safe to show to the user."""


_TAG_RE = re.compile(r'\["([^"]+)"(=|~)"([^"]*)"\]')


def parse_selector(selector: str) -> list[tuple[str, str, str]]:
    """'["sport"~"boxing"]["name"="x"]' -> [("sport", "~", "boxing"), ("name", "=", "x")]."""
    parts = _TAG_RE.findall(selector)
    if not parts or "".join(f'["{k}"{op}"{v}"]' for k, op, v in parts) != selector:
        raise ValueError(f"Unsupported Overpass selector: {selector}")
    return parts


def selector_matches(tags: dict, selector: str) -> bool:
    for key, op, value in parse_selector(selector):
        actual = tags.get(key)
        if actual is None:
            return False
        if op == "=" and actual != value:
            return False
        if op == "~" and not re.search(value, actual):  # Overpass regex is unanchored, like re.search
            return False
    return True


def match_categories(tags: dict) -> list[str]:
    """Every category whose filters match these tags (a place can be a gym AND a boxing club)."""
    return [cat for cat, selectors in CATEGORY_OSM_FILTERS.items() if any(selector_matches(tags, s) for s in selectors)]


def build_query(
    categories: Iterable[str],
    *,
    area_id: int | None = None,
    bbox: list[float] | None = None,
    timeout: int = 120,
) -> str:
    """Area and bbox can be combined: "inside this country AND inside this tile"."""
    selectors = list(dict.fromkeys(s for c in categories for s in CATEGORY_OSM_FILTERS[c]))
    if not selectors:
        raise ValueError("No categories selected")
    if not area_id and not bbox:
        raise ValueError("Need an area id or a bounding box")
    head = f"area(id:{area_id})->.searchArea;\n" if area_id else ""
    scope = "(area.searchArea)" if area_id else ""
    if bbox:
        south, west, north, east = bbox
        scope += f"({south},{west},{north},{east})"
    # ["name"]: only named places; an unnamed pin isn't a usable lead.
    body = "\n".join(f'  nwr{s}["name"]{scope};' for s in selectors)
    return f"[out:json][timeout:{timeout}];\n{head}(\n{body}\n);\nout center tags;"


class OverpassBusy(OverpassError):
    """Server is busy or rate-limiting us: wait and retry the same query."""


class OverpassTooHeavy(OverpassError):
    """The query is too big for the server: split the area into smaller pieces."""


BUSY_STATUS = {429, 502, 503}
HEAVY_STATUS = {504}
HEAVY_REMARKS = ("runtime error", "timed out", "out of memory")


def run_query(query: str, *, client: httpx.Client, url: str) -> list[dict]:
    """One request, no retries. The exception type tells the caller what to do next."""
    try:
        resp = client.post(url, data={"data": query})
    except (httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        # Never got connected: the server is down or overloaded, the query size doesn't matter.
        raise OverpassBusy("Couldn't reach the OpenStreetMap search service.") from exc
    except httpx.TimeoutException as exc:
        raise OverpassTooHeavy("The search took too long.") from exc
    except (httpx.ReadError, httpx.RemoteProtocolError) as exc:
        # The server dropped the connection mid-answer: this happens on very heavy queries.
        raise OverpassTooHeavy("The server closed the connection.") from exc
    except httpx.TransportError as exc:  # can't connect at all: network or server down
        raise OverpassBusy("Couldn't reach the OpenStreetMap search service.") from exc

    if resp.status_code == 200:
        data = resp.json()
        remark = (data.get("remark") or "").lower()
        if any(r in remark for r in HEAVY_REMARKS):
            raise OverpassTooHeavy("The search was too big for the server.")
        return data.get("elements", [])
    if resp.status_code in BUSY_STATUS:
        raise OverpassBusy("The OpenStreetMap search service is busy. Try again in a few minutes.")
    if resp.status_code in HEAVY_STATUS:
        raise OverpassTooHeavy("The search took too long.")
    raise OverpassError(f"OpenStreetMap search failed (HTTP {resp.status_code}).")


_SLOTS_FREE = re.compile(r"(\d+) slots? available now")
_SLOT_IN = re.compile(r"in (-?\d+) seconds")


def wait_for_slot(
    client: httpx.Client, status_url: str, sleep: Callable[[float], None], max_wait: float = 120
) -> float | None:
    """Ask the server when our next query slot is free, and wait exactly that long.

    Returns the seconds waited, or None if the status page couldn't be read
    (an overloaded server often can't answer it). Then we don't wait: the query
    itself will report "busy" and be retried.
    """
    try:
        resp = client.get(status_url, timeout=5)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    text = resp.text
    free = _SLOTS_FREE.search(text)
    if free and int(free.group(1)) > 0:
        return 0
    waits = [int(x) for x in _SLOT_IN.findall(text)]
    if not waits:
        return 0
    seconds = min(max(min(waits), 0) + 1, max_wait)
    sleep(seconds)
    return seconds
