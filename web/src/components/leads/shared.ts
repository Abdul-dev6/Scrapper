import { CATEGORIES } from "@/lib/categories";
import type { EmailChecks, PhoneChecks } from "@/lib/contactStatus";

export type Social = { url?: string; sourceUrl?: string; sourceType?: string };
export type LeadRow = {
  _id: string;
  name: string;
  categories?: string[];
  address?: { city?: string; countryCode?: string };
  website?: { url?: string; domain?: string; reachable?: boolean };
  emails?: { value: string; mxValid?: boolean; checks?: EmailChecks }[];
  phones?: { e164?: string; raw?: string; checks?: PhoneChecks }[];
  whatsapp?: { e164?: string }[];
  socials?: Record<string, Social | undefined>;
  contactStatus?: string; // shown through effectiveContactStatus() (handles pre-migration values)
  confidence?: number;
  saved?: boolean;
  status?: "new" | "contacted" | "not_interested";
};

export const SOCIAL_KEYS = ["instagram", "facebook", "linkedin", "youtube", "tiktok"] as const;
export const SOCIAL_SHORT: Record<string, string> = { instagram: "IG", facebook: "FB", linkedin: "in", youtube: "YT", tiktok: "TT" };
export const STATUS_LABEL = { new: "New", contacted: "Contacted", not_interested: "Not interested" } as const;
// Only "verified" means a person checked it. Automatic checks are "Checked automatically" at most.
export const CONTACT_LABEL = {
  none: "No contact",
  unchecked: "Unchecked",
  checked: "Checked automatically",
  verified: "Verified by you",
} as const;
export const CONTACT_STYLE = { none: "text-muted", unchecked: "text-warn", checked: "text-ink", verified: "text-ok" } as const;

export const categoryLabel = (id: string) => CATEGORIES.find((c) => c.id === id)?.label ?? id;

/**
 * Only http(s) links are rendered. Data comes from OpenStreetMap and from websites, so a
 * "javascript:..." value must never become a clickable link (that would be XSS).
 */
export function safeHref(url?: string | null): string | undefined {
  if (!url) return undefined;
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : undefined;
  } catch {
    return undefined;
  }
}

export const countryName = (() => {
  let names: Intl.DisplayNames | null = null;
  return (code?: string) => {
    if (!code) return "";
    try {
      names ??= new Intl.DisplayNames(["en"], { type: "region" });
      return names.of(code) ?? code;
    } catch {
      return code;
    }
  };
})();

export async function errorMessage(res: Response | null): Promise<string> {
  if (!res) return "Couldn't reach the server. Check your connection.";
  const body = await res.json().catch(() => null);
  return body?.error?.message ?? "Something went wrong.";
}
