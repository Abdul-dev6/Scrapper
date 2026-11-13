"""Contact check statuses. Machine checks are NEVER called "verified".

Per email:  checks = {syntaxValid, mx: "valid" | "no_mx" | "unknown", checkedAt, manuallyVerified, ...}
Per phone:  checks = {formatValid, checkedAt, manuallyVerified, ...}
(`mxValid` is still written next to `checks` while the migration runs; it will be removed later.)

Lead-level contactStatus:
  verified   a person marked at least one email/phone as verified (the ONLY way to get "verified")
  checked    automatic checks passed: an email whose domain has a mail server, or a valid phone number
  unchecked  contacts exist, but none passed an automatic check (yet)
  none       no email, phone or WhatsApp
Must stay identical to web/src/lib/contactStatus.ts.
"""

from __future__ import annotations

from datetime import datetime

MANUAL_FIELDS = ("manuallyVerified", "manuallyVerifiedAt", "manuallyVerifiedBy")
CONTACTS_VERSION = 2  # every email/phone has `checks`; the web migration skips leads at this version


def mx_label(valid: bool | None) -> str:
    return "unknown" if valid is None else ("valid" if valid else "no_mx")


def email_checks(previous: dict | None, *, mx: bool | None, now: datetime) -> dict:
    """Stored emails always passed the syntax filter (clean_email / EMAIL_RE) before being saved."""
    checks = {"syntaxValid": True, "mx": mx_label(mx), "checkedAt": now, "manuallyVerified": False}
    for f in MANUAL_FIELDS:  # a person's decision survives every automatic re-check
        if previous and f in previous:
            checks[f] = previous[f]
    return checks


def phone_checks(previous: dict | None, *, e164: str | None, now: datetime) -> dict:
    checks = {"formatValid": bool(e164), "checkedAt": now, "manuallyVerified": False}
    for f in MANUAL_FIELDS:
        if previous and f in previous:
            checks[f] = previous[f]
    return checks


def email_key(item: dict) -> str:
    return str(item.get("value") or "").strip().lower()


def phone_key(item: dict) -> str:
    return str(item.get("e164") or item.get("raw") or "").strip()


def manual_by_key(items: list | None, key) -> dict[str, dict]:
    """value -> previous checks, for items a person has touched (so a refresh can't lose them)."""
    out = {}
    for item in items or []:
        checks = item.get("checks") or {}
        if any(f in checks for f in MANUAL_FIELDS) and key(item):
            out[key(item)] = checks
    return out


def carry_manual(items: list[dict], previous: dict[str, dict], key) -> None:
    """Re-apply manual verification to items rebuilt from a fresh source (same value = same contact)."""
    for item in items:
        prev = previous.get(key(item))
        if prev:
            checks = dict(item.get("checks") or {})
            for f in MANUAL_FIELDS:
                if f in prev:
                    checks[f] = prev[f]
            item["checks"] = checks


def contact_status(emails: list | None, phones: list | None, whatsapp: list | None = None) -> str:
    emails, phones, whatsapp = emails or [], phones or [], whatsapp or []
    if not (emails or phones or whatsapp):
        return "none"
    if any((i.get("checks") or {}).get("manuallyVerified") for i in [*emails, *phones]):
        return "verified"
    mx_ok = any((e.get("checks") or {}).get("mx") == "valid" or e.get("mxValid") is True for e in emails)
    phone_ok = any((p.get("checks") or {}).get("formatValid") or p.get("e164") for p in phones) or \
        any(w.get("e164") for w in whatsapp)
    return "checked" if (mx_ok or phone_ok) else "unchecked"
