"""Checks that never contact anyone.

Emails: syntax + "does this domain accept email at all" (an MX record in DNS).
We never connect to the mail server (no SMTP probing).
Phones: parsed with Google's libphonenumber into one international format (E.164).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable

import dns.asyncresolver
import dns.exception
import dns.resolver
import phonenumbers

from ..osm.mapping import EMAIL_RE, GENERIC_EMAIL_LOCALS

# Text that looks like an email but isn't one (image names, placeholders, tracking services).
FAKE_EMAIL_DOMAINS = {
    "example.com", "example.org", "example.net", "domain.com", "email.com", "yourdomain.com",
    "yoursite.com", "sentry.io", "wixpress.com", "sentry.wixpress.com", "sentry-next.wixpress.com",
    "mysite.com", "test.com", "company.com",
}
# Mailboxes that exist for legal/technical reasons; useless (and unwelcome) as sales contacts.
ROLE_ONLY = re.compile(
    r"^(no-?reply|do-?not-?reply|privacy|gdpr|dpo|data-?protection|dataprotection|complaints?|safeguarding|"
    r"abuse|postmaster|hostmaster|webmaster|careers|jobs|recruitment|hr|legal|unsubscribe|bounce)([._-]|$)",
    re.IGNORECASE,
)
FILE_ENDINGS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js", ".avif", ".ico")


def clean_email(raw: str) -> str | None:
    value = raw.strip().strip(".,;:()<>[]'\"")
    if not EMAIL_RE.match(value) or len(value) > 254:
        return None
    local, domain = value.rsplit("@", 1)
    domain = domain.lower()
    if domain in FAKE_EMAIL_DOMAINS or domain.endswith(FILE_ENDINGS) or "@2x" in value or "@3x" in value:
        return None
    if re.fullmatch(r"[0-9a-f]{20,}", local.lower()):  # random ids from tracking scripts
        return None
    if ROLE_ONLY.match(local):
        return None
    return f"{local}@{domain}"


def email_kind(email: str) -> str:
    return "generic" if email.split("@")[0].lower() in GENERIC_EMAIL_LOCALS else "named"


def phone_e164(raw: str, region: str | None) -> str | None:
    """'+44 20 7946 0000' -> '+442079460000'. None if it isn't a valid number."""
    try:
        number = phonenumbers.parse(raw, (region or "").upper() or None)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


Resolve = Callable[[str], Awaitable[bool | None]]


async def dns_has_mx(domain: str) -> bool | None:
    """True: has MX. False: domain says 'no email here'. None: couldn't tell (timeout)."""
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = 5
    try:
        answer = await resolver.resolve(domain, "MX")
        return any(str(r.exchange) not in (".", "") for r in answer)  # "0 ." = null MX: no email
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        try:  # no MX: mail may still go to the domain's own address (RFC 5321)
            await resolver.resolve(domain, "A")
            return True
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return False
        except dns.exception.DNSException:
            return None
    except dns.exception.DNSException:
        return None


class MxChecker:
    """One DNS lookup per email domain per job."""

    def __init__(self, resolve: Resolve = dns_has_mx):
        self.resolve = resolve
        self._cache: dict[str, asyncio.Task] = {}

    async def check(self, email: str) -> bool | None:
        domain = email.rsplit("@", 1)[1].lower()
        if domain not in self._cache:
            self._cache[domain] = asyncio.ensure_future(self.resolve(domain))
        return await self._cache[domain]
