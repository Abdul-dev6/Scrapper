"""Merge a crawled website into a lead, and verify every contact value on the lead.

  - Website values replace the previous website values (the site is the newest truth),
    but only when the site was reachable this time: a site that is down keeps its old data.
  - OSM values and user fields (saved, status, notes) are never removed.
  - The same email/phone found twice is stored once (first source wins).
"""

from __future__ import annotations

from datetime import datetime

from .. import contacts
from .site import SiteResult
from .verify import MxChecker, clean_email, email_kind, phone_e164

WEBSITE = "website"


def _base(items: list | None, reachable: bool) -> list[dict]:
    items = [dict(i) for i in items or []]
    return [i for i in items if i.get("sourceType") != WEBSITE] if reachable else items


async def build_update(lead: dict, result: SiteResult, *, now: datetime, mx: MxChecker) -> dict:
    """The $set for this lead."""
    region = (lead.get("address") or {}).get("countryCode")
    ok = result.reachable
    s: dict = {
        "website.reachable": ok,
        "website.checkedAt": now,
        "updatedAt": now,
        "verifiedAt": now,
    }

    # A person's "verified" mark must survive the refresh, even when website values are rebuilt.
    manual_emails = contacts.manual_by_key(lead.get("emails"), contacts.email_key)
    manual_phones = contacts.manual_by_key(lead.get("phones"), contacts.phone_key)

    # Emails
    emails = _base(lead.get("emails"), ok)
    known = {e["value"].lower() for e in emails}
    if ok:
        for v in result.emails:
            email = clean_email(v.value)
            if email and email.lower() not in known:
                known.add(email.lower())
                emails.append({"value": email, "kind": email_kind(email), "sourceUrl": v.source_url, "sourceType": WEBSITE})
    for e in emails:
        valid = await mx.check(e["value"])
        if valid is None:
            e.pop("mxValid", None)  # couldn't tell: leave it unknown, not "invalid"
        else:
            e["mxValid"] = valid  # legacy field, kept until the migration is finished
        e["checks"] = contacts.email_checks(e.get("checks"), mx=valid, now=now)
    contacts.carry_manual(emails, manual_emails, contacts.email_key)

    # Phones (OSM phones get their international format here too)
    phones = _base(lead.get("phones"), ok)
    for p in phones:
        if not p.get("e164"):
            e164 = phone_e164(p.get("raw", ""), region)
            if e164:
                p["e164"] = e164
    have = {p.get("e164") or p.get("raw") for p in phones}
    if ok:
        for v in result.phones:
            e164 = phone_e164(v.value, region)
            if e164 and e164 not in have:  # website numbers must be real numbers; skip "tel:0" etc.
                have.add(e164)
                phones.append({"e164": e164, "raw": v.value, "sourceUrl": v.source_url, "sourceType": WEBSITE})
    for p in phones:
        p["checks"] = contacts.phone_checks(p.get("checks"), e164=p.get("e164"), now=now)
    contacts.carry_manual(phones, manual_phones, contacts.phone_key)

    # WhatsApp
    whatsapp = _base(lead.get("whatsapp"), ok)
    have_wa = {w.get("e164") for w in whatsapp if w.get("e164")}
    if ok:
        for v in result.whatsapp:
            e164 = phone_e164(v.value, region)
            if e164 and e164 not in have_wa:
                have_wa.add(e164)
                whatsapp.append({"e164": e164, "url": f"https://wa.me/{e164[1:]}", "sourceUrl": v.source_url,
                                 "sourceType": WEBSITE})

    s.update(emails=emails, phones=phones, whatsapp=whatsapp)

    # Socials: an OSM contact:* link stays; a website link fills a gap or refreshes an older website link.
    socials = dict(lead.get("socials") or {})
    if ok:
        for kind, v in result.socials.items():
            current = socials.get(kind)
            if not current or current.get("sourceType") == WEBSITE:
                socials[kind] = {"url": v.value, "sourceUrl": v.source_url, "sourceType": WEBSITE}
                s[f"socials.{kind}"] = socials[kind]

    # Sources: one entry per page we read
    if ok:
        pages = set(result.pages)
        s["sources"] = [x for x in lead.get("sources", []) if x.get("url") not in pages] + [
            {"url": p, "type": WEBSITE, "fetchedAt": now} for p in result.pages
        ]

    has_mx_email = any(e.get("mxValid") for e in emails)
    has_e164 = any(p.get("e164") for p in phones) or bool(whatsapp)
    # "verified" only ever comes from a person; automatic checks give "checked" at most.
    s["contactStatus"] = contacts.contact_status(emails, phones, whatsapp)
    s["contactsVersion"] = contacts.CONTACTS_VERSION  # all emails/phones were just given `checks`
    s["confidence"] = round(
        0.35 * has_mx_email
        + 0.25 * has_e164
        + 0.15 * ok
        + 0.10 * bool(socials)
        + 0.15 * any(i.get("sourceType") == WEBSITE for i in [*emails, *phones, *whatsapp]),
        2,
    )
    return s


def as_lead(lead: dict, update: dict) -> dict:
    """The lead as it will look after `update` (for the suppression check)."""
    merged = {**lead, **{k: v for k, v in update.items() if "." not in k}}
    return merged
