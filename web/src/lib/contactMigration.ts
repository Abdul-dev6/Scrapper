// Contact-status migration (Phase 1): pure function, used by scripts/migrate-contact-status.ts and tests.
//   contactStatus "partial" -> "unchecked"; "verified" (old meaning: automatic checks passed) -> "checked"
//   emails[].mxValid true/false/missing -> checks.mx "valid"/"no_mx"/"unknown", checks.syntaxValid = true
//   phones[] -> checks.formatValid = has a valid international (E.164) number
//   manuallyVerified = false everywhere (nobody has verified anything by hand before this feature)
// Existing `checks` (e.g. written by the new worker) are kept. `mxValid` is kept for now.
import { computeContactStatus, type ContactLead, type EmailLike, type PhoneLike } from "./contactStatus";

export const CONTACTS_VERSION = 2;

type Doc = ContactLead & { verifiedAt?: Date | null; contactsVersion?: number };

export function migrateLeadContacts(lead: Doc): { set: Record<string, unknown>; from: string; to: string } {
  const checkedAt = lead.verifiedAt ?? undefined;
  const emails = (lead.emails ?? []).map((e: EmailLike) => {
    const c = e.checks ?? {};
    const mx = c.mx ?? (e.mxValid === true ? "valid" : e.mxValid === false ? "no_mx" : "unknown");
    return {
      ...e,
      checks: {
        ...c,
        syntaxValid: c.syntaxValid ?? true, // every stored email passed the syntax filter before saving
        mx,
        ...(c.checkedAt ?? checkedAt ? { checkedAt: c.checkedAt ?? checkedAt } : {}),
        manuallyVerified: c.manuallyVerified ?? false,
      },
    };
  });
  const phones = (lead.phones ?? []).map((p: PhoneLike) => {
    const c = p.checks ?? {};
    return {
      ...p,
      checks: {
        ...c,
        formatValid: c.formatValid ?? Boolean(p.e164),
        ...(c.checkedAt ?? checkedAt ? { checkedAt: c.checkedAt ?? checkedAt } : {}),
        manuallyVerified: c.manuallyVerified ?? false,
      },
    };
  });
  const to = computeContactStatus({ ...lead, emails, phones });
  return {
    set: { emails, phones, contactStatus: to, contactsVersion: CONTACTS_VERSION },
    from: lead.contactStatus ?? "(none)",
    to,
  };
}
