"""Find PUBLISHED contact details in one HTML page.

Order of trust:
  1. JSON-LD (structured "business card" data many sites add for search engines)
  2. Links: mailto:, tel:, WhatsApp, social profile links
  3. Plain text emails in the visible page text

Nothing is guessed: only values written on the page are returned.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..osm.mapping import host_of, registrable_domain

# Bounded like real addresses (RFC 5321: local part <= 64, labels <= 63, domain <= 253). Unbounded `+`
# made findall QUADRATIC on long letter runs without "@" (base64 blobs, minified text): a 2 MB page
# took minutes inside the crawler's event loop, stalling every other site and the job heartbeat.
EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}")
_BEFORE_AT, _AFTER_AT = 64, 9 * 64 + 25  # the longest possible match on each side of "@"
_LOCAL_TAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}\Z")  # used with endpos = the "@"


def emails_in_text(text: str) -> list[str]:
    """EMAIL_IN_TEXT.findall(text), but only looking near each "@" (linear in the text length)."""
    out: list[str] = []
    end = 0  # matches never overlap: continue after the previous one
    for at in (m.start() for m in re.finditer("@", text)):
        if at < end:
            continue
        # The local part is the run of address characters right before this "@" (at most 64 of them):
        # one anchored match from where that run starts. The part after "@" doesn't depend on where the
        # local part starts, so this is the match findall would find (leftmost start).
        local = _LOCAL_TAIL.search(text, max(end, at - _BEFORE_AT), at)
        if local is None:
            continue
        m = EMAIL_IN_TEXT.match(text, local.start(), min(len(text), at + _AFTER_AT))
        if m:
            out.append(m.group())
            end = m.end()
    return out

SOCIAL_SITES = {
    "instagram": ("instagram.com",),
    "facebook": ("facebook.com", "fb.com", "fb.me"),
    "linkedin": ("linkedin.com",),
    "youtube": ("youtube.com",),
    "tiktok": ("tiktok.com",),
}
# Links that are about a post or a "share this" button, not the business's own profile.
NOT_A_PROFILE = re.compile(
    r"/(sharer|share|sharearticle|dialog|plugins|tr|intent|p|reel|reels|explore|watch|embed|"
    r"hashtag|events|groups|login|signup|privacy|legal|policies|help|search|feed|posts|photos|videos)(/|\.php|$|\?)",
    re.IGNORECASE,
)


@dataclass
class Found:
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    whatsapp: list[str] = field(default_factory=list)  # phone numbers (digits, maybe with +)
    socials: dict[str, str] = field(default_factory=dict)
    links: list[tuple[str, str, bool]] = field(default_factory=list)  # (url, link text, in footer)
    used_jsonld: bool = False

    def add(self, kind: str, value: str) -> None:
        bucket = getattr(self, kind)
        if value and value not in bucket:
            bucket.append(value)


def social_profile(url: str) -> tuple[str, str] | None:
    """('instagram', clean url) for a link to a profile/page, else None."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    domain = registrable_domain(host_of(url))
    for kind, domains in SOCIAL_SITES.items():
        if domain in domains:
            path = parts.path.rstrip("/")
            if not path or NOT_A_PROFILE.search(parts.path):
                return None
            if kind == "tiktok" and not path.startswith("/@"):
                return None
            if kind == "linkedin" and not re.match(r"^/(company|school|in)/[^/]+", path):
                return None
            if path.endswith("profile.php"):  # facebook.com/profile.php?id=123: the id is the profile
                page_id = (parse_qs(parts.query).get("id") or [""])[0]
                return (kind, f"https://{parts.hostname.lower()}{path}?id={page_id}") if page_id.isdigit() else None
            return kind, f"https://{parts.hostname.lower()}{path}"
    return None


def whatsapp_number(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in ("wa.me", "www.wa.me"):
        number = parts.path.strip("/")
    elif host.endswith("whatsapp.com") or parts.scheme == "whatsapp":
        number = (parse_qs(parts.query).get("phone") or [""])[0]
    else:
        return None
    digits = re.sub(r"\D", "", number)
    return f"+{digits}" if 7 <= len(digits) <= 15 else None


def _walk_jsonld(node, found: Found) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_jsonld(item, found)
        return
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        k = key.lower()
        values = value if isinstance(value, list) else [value]
        if k == "email":
            for v in values:
                if isinstance(v, str):
                    found.add("emails", unquote(v).removeprefix("mailto:").strip())
        elif k == "telephone":
            for v in values:
                if isinstance(v, str):
                    found.add("phones", v.strip())
        elif k == "sameas":
            for v in values:
                if isinstance(v, str):
                    profile = social_profile(v.strip())
                    if profile:
                        found.socials.setdefault(*profile)
        elif isinstance(value, (dict, list)):
            _walk_jsonld(value, found)


def extract(html: str, page_url: str) -> Found:
    soup = BeautifulSoup(html, "html.parser")
    found = Found()

    # 1. JSON-LD
    for script in soup.find_all("script", type=lambda t: t and "ld+json" in t.lower()):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue  # broken JSON-LD is common; skip it, the rest of the page still counts
        found.used_jsonld = True
        _walk_jsonld(data, found)

    # 2. Links
    footer_links = set()
    for footer in soup.find_all("footer"):
        footer_links.update(id(a) for a in footer.find_all("a"))
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if low.startswith("mailto:"):
            address = unquote(href[7:].split("?")[0]).strip()
            for part in address.split(","):
                found.add("emails", part.strip())
            continue
        if low.startswith("tel:"):
            found.add("phones", unquote(href[4:]).strip())
            continue
        url = urljoin(page_url, href)
        number = whatsapp_number(url)
        if number:
            found.add("whatsapp", number)
            continue
        profile = social_profile(url)
        if profile:
            found.socials.setdefault(*profile)
            continue
        if url.startswith(("http://", "https://")):
            text = " ".join(a.get_text(" ", strip=True).split())[:100]
            found.links.append((url.split("#")[0], text, id(a) in footer_links))

    # 3. Emails written in the visible text (scripts and styles removed first)
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    for match in emails_in_text(soup.get_text(" ")):
        found.add("emails", match)

    return found
