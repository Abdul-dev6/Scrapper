"""Crawl one business website: the home page, then the most promising contact/about pages.

At most `max_pages` pages per site. Only pages on the business's own domain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..osm.mapping import host_of, registrable_domain
from .extract import Found, extract
from .fetcher import Fetcher, FetchError

# Words in a link's URL or text that point to contact details, in common languages.
# Weight 3: contact pages. 2: legal/imprint pages (often list email + phone). 1: about/team.
LINK_WORDS = [
    (3, r"contact|kontakt|contacto|contato|contatti|contactez|contacteer|iletisim|kapcsolat|yhteystiedot|"
        r"kontakta|kontaktai|связ|контакт|اتصل|تواصل|お問い合わせ|联系"),
    (2, r"impressum|imprint|legal-notice|mentions-legales|aviso-legal|colofon"),
    (1, r"about|uber-uns|ueber-uns|nosotros|quienes-somos|chi-siamo|qui-sommes|over-ons|o-nas|team|"
        r"location|find-us|visit|membership|join"),
]
SKIP_ENDINGS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip", ".mp4", ".mp3",
                ".doc", ".docx", ".xls", ".xlsx", ".ics")
SKIP_PATHS = re.compile(r"/(wp-admin|wp-login|login|signin|sign-in|account|cart|checkout|my-account|feed)(/|$)",
                        re.IGNORECASE)
# Legal fine print lists data-protection / complaints addresses, not the business contact.
# (Impressum is different: in Germany/Austria it is THE page with the official email and phone.)
FINE_PRINT = re.compile(r"privacy|cookie|terms|conditions|complaint|agreement|gdpr|policy|policies|"
                        r"datenschutz|agb|safeguarding|careers|jobs|vacanc|recruit", re.IGNORECASE)


@dataclass
class Value:
    value: str
    source_url: str


@dataclass
class SiteResult:
    url: str
    reachable: bool
    final_url: str = ""
    pages: list[str] = field(default_factory=list)
    emails: list[Value] = field(default_factory=list)
    phones: list[Value] = field(default_factory=list)
    whatsapp: list[Value] = field(default_factory=list)
    socials: dict[str, Value] = field(default_factory=dict)
    error: str = ""

    def take(self, found: Found, page_url: str) -> None:
        for kind in ("emails", "phones", "whatsapp"):
            bucket: list[Value] = getattr(self, kind)
            known = {v.value.lower() for v in bucket}
            for value in getattr(found, kind):
                if value.lower() not in known:
                    known.add(value.lower())
                    bucket.append(Value(value, page_url))
        for kind, url in found.socials.items():
            self.socials.setdefault(kind, Value(url, page_url))


def link_score(url: str, text: str, in_footer: bool) -> int:
    haystack = f"{urlsplit(url).path} {text}".lower()
    score = 0
    for weight, pattern in LINK_WORDS:
        if re.search(pattern, haystack):
            score = max(score, weight)
    if score and in_footer:
        score += 1  # footer links to "Contact"/"Impressum" are almost always the real ones
    return score


def pick_pages(found: Found, site_domain: str, visited: set[str], limit: int) -> list[str]:
    ranked: dict[str, int] = {}
    for url, text, in_footer in found.links:
        parts = urlsplit(url)
        if registrable_domain(host_of(url)) != site_domain:
            continue
        if parts.path.lower().endswith(SKIP_ENDINGS) or SKIP_PATHS.search(parts.path) or FINE_PRINT.search(parts.path):
            continue
        clean = f"{parts.scheme}://{parts.netloc}{parts.path or '/'}" + (f"?{parts.query}" if parts.query else "")
        if clean.rstrip("/") in visited:
            continue
        score = link_score(url, text, in_footer)
        if score:
            ranked[clean] = max(score, ranked.get(clean, 0))
    return [u for u, _ in sorted(ranked.items(), key=lambda kv: -kv[1])][:limit]


async def crawl_site(fetcher: Fetcher, url: str, *, max_pages: int = 5) -> SiteResult:
    try:
        home = await fetcher.fetch(url)
    except FetchError as exc:
        return SiteResult(url, reachable=False, error=str(exc))

    result = SiteResult(url, reachable=True, final_url=home.url, pages=[home.url])
    found = extract(home.html, home.url)
    result.take(found, home.url)
    # The site may redirect to its real domain (gym.com -> gym.co.uk): follow links on that one.
    site_domain = registrable_domain(host_of(home.url))
    visited = {url.rstrip("/"), home.url.rstrip("/")}

    for page_url in pick_pages(found, site_domain, visited, max_pages - 1):
        if result.emails and len(result.pages) >= 2 and result.phones:
            break  # found the essentials on the contact page; don't load more pages than needed
        try:
            page = await fetcher.fetch(page_url)
        except FetchError:
            continue  # one missing page is normal; keep the rest
        visited.add(page_url.rstrip("/"))
        result.pages.append(page.url)
        result.take(extract(page.html, page.url), page.url)
    return result
