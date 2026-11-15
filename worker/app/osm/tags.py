"""Category selectors: which OSM tags make a place a gym, boxing club, ... (moved from overpass.py).

The selectors in categories.py are written in Overpass tag-filter syntax (e.g. '["sport"~"boxing"]');
they are evaluated locally against each OSM object's tags, both when filtering Geofabrik extracts and
when mapping an element to a lead. Behaviour is unchanged from the Overpass version.
"""

from __future__ import annotations

import re
from functools import lru_cache

from ..categories import CATEGORY_OSM_FILTERS

_TAG_RE = re.compile(r'\["([^"]+)"(=|~)"([^"]*)"\]')


@lru_cache(maxsize=None)
def parse_selector(selector: str) -> tuple[tuple[str, str, str], ...]:
    """'["sport"~"boxing"]["name"="x"]' -> (("sport", "~", "boxing"), ("name", "=", "x"))."""
    parts = _TAG_RE.findall(selector)
    if not parts or "".join(f'["{k}"{op}"{v}"]' for k, op, v in parts) != selector:
        raise ValueError(f"Unsupported tag selector: {selector}")
    return tuple(parts)


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


def selector_keys() -> frozenset[str]:
    """Every tag key any selector looks at (a cheap first filter while reading a big extract)."""
    return frozenset(k for sels in CATEGORY_OSM_FILTERS.values() for s in sels for k, _, _ in parse_selector(s))


def is_wanted(tags: dict) -> bool:
    """A usable lead: has a name (unnamed pins aren't leads) and matches at least one category.
    Same rule as the old Overpass query's ["name"] + selectors."""
    return bool((tags.get("name") or "").strip()) and bool(match_categories(tags))
