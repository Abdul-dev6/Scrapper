// Contact check statuses. Must stay identical to worker/app/contacts.py.
//   verified   a PERSON marked at least one email/phone as verified (the only way to get "verified")
//   checked    automatic checks passed (mail server found for an email, or a valid phone number)
//   unchecked  contacts exist, none passed an automatic check
//   none       no contacts
// Legacy values (before the migration): "partial" = unchecked, "verified" without any manual mark = checked.

export const CONTACT_STATUSES = ["none", "unchecked", "checked", "verified"] as const;
export type ContactStatus = (typeof CONTACT_STATUSES)[number];
export const LEGACY_CONTACT_STATUSES = ["partial"] as const;

export type EmailChecks = {
  syntaxValid?: boolean | null;
  mx?: "valid" | "no_mx" | "unknown" | null;
  checkedAt?: Date | string | null;
  manuallyVerified?: boolean | null;
  manuallyVerifiedAt?: Date | string | null;
  manuallyVerifiedBy?: unknown;
};
export type PhoneChecks = {
  formatValid?: boolean | null;
  checkedAt?: Date | string | null;
  manuallyVerified?: boolean | null;
  manuallyVerifiedAt?: Date | string | null;
  manuallyVerifiedBy?: unknown;
};
export type EmailLike = { value?: string | null; mxValid?: boolean | null; checks?: EmailChecks | null };
export type PhoneLike = { e164?: string | null; raw?: string | null; checks?: PhoneChecks | null };
export type ContactLead = {
  contactStatus?: string | null;
  emails?: EmailLike[] | null;
  phones?: PhoneLike[] | null;
  whatsapp?: { e164?: string | null }[] | null;
};

/** The MX result of an email, reading the new `checks` first and the legacy `mxValid` second. */
export function emailMx(e: EmailLike): "valid" | "no_mx" | "unknown" {
  if (e.checks?.mx) return e.checks.mx;
  if (e.mxValid === true) return "valid";
  if (e.mxValid === false) return "no_mx";
  return "unknown";
}

export const phoneFormatValid = (p: PhoneLike) => Boolean(p.checks?.formatValid ?? p.e164);

export function computeContactStatus(l: ContactLead): ContactStatus {
  const emails = l.emails ?? [];
  const phones = l.phones ?? [];
  const whatsapp = l.whatsapp ?? [];
  if (!emails.length && !phones.length && !whatsapp.length) return "none";
  if ([...emails, ...phones].some((c) => c.checks?.manuallyVerified)) return "verified";
  const mxOk = emails.some((e) => emailMx(e) === "valid");
  const phoneOk = phones.some(phoneFormatValid) || whatsapp.some((w) => Boolean(w.e164));
  return mxOk || phoneOk ? "checked" : "unchecked";
}

/** What to SHOW for a stored lead, also for records not migrated yet. */
export function effectiveContactStatus(l: ContactLead): ContactStatus {
  const s = l.contactStatus;
  if (s === "partial") return "unchecked";
  const manual = [...(l.emails ?? []), ...(l.phones ?? [])].some((c) => c.checks?.manuallyVerified);
  if (s === "verified" && !manual) {
    return "checked"; // legacy "verified" meant "automatic checks passed"
  }
  return (CONTACT_STATUSES as readonly string[]).includes(s ?? "") ? (s as ContactStatus) : computeContactStatus(l);
}

const MANUAL_EMAIL = { "emails.checks.manuallyVerified": true };
const MANUAL_PHONE = { "phones.checks.manuallyVerified": true };

/** MongoDB filter for a status, correct before AND after the migration. */
export function contactStatusFilter(status: ContactStatus): Record<string, unknown> {
  switch (status) {
    case "verified":
      return { contactStatus: "verified", $or: [MANUAL_EMAIL, MANUAL_PHONE] };
    case "checked":
      return {
        $or: [
          { contactStatus: "checked" },
          { contactStatus: "verified", "emails.checks.manuallyVerified": { $ne: true }, "phones.checks.manuallyVerified": { $ne: true } },
        ],
      };
    case "unchecked":
      return { contactStatus: { $in: ["unchecked", "partial"] } };
    default:
      return { contactStatus: "none" };
  }
}

export type VerifyContactInput = { kind: "email" | "phone"; value: string; verified: boolean };

/**
 * Apply a person's verification to one email/phone (matched by value). Returns the new arrays and
 * status, or null if the lead has no such contact. Pure, so it can be unit-tested.
 */
export function applyManualVerification<L extends ContactLead>(
  lead: L,
  input: VerifyContactInput,
  by: string,
  now: Date
): { emails: EmailLike[]; phones: PhoneLike[]; contactStatus: ContactStatus } | null {
  const target = input.value.trim().toLowerCase();
  let found = false;
  const mark = <T extends { checks?: Record<string, unknown> | null }>(item: T): T => {
    found = true;
    const checks = { ...(item.checks ?? {}) } as Record<string, unknown>;
    if (input.verified) {
      checks.manuallyVerified = true;
      checks.manuallyVerifiedAt = now;
      checks.manuallyVerifiedBy = by;
    } else {
      checks.manuallyVerified = false;
      delete checks.manuallyVerifiedAt;
      delete checks.manuallyVerifiedBy;
    }
    return { ...item, checks };
  };
  const emails = (lead.emails ?? []).map((e) =>
    input.kind === "email" && (e.value ?? "").trim().toLowerCase() === target ? mark(e as EmailLike & { checks?: Record<string, unknown> }) : e
  );
  const digits = (s?: string | null) => (s ?? "").replace(/[^\d+]/g, "");
  const phones = (lead.phones ?? []).map((p) =>
    input.kind === "phone" && (digits(p.e164) === digits(target) || digits(p.raw) === digits(target))
      ? mark(p as PhoneLike & { checks?: Record<string, unknown> })
      : p
  );
  if (!found) return null;
  return { emails, phones, contactStatus: computeContactStatus({ ...lead, emails, phones }) };
}
