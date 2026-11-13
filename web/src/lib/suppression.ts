import { createHash } from "node:crypto";
import type { Types } from "mongoose";
import { Lead, Suppression } from "@/models";

// MUST match worker/app/suppression.py exactly, or the worker and the web app would
// disagree about who is on the list.
export type SuppressionType = "email" | "phone" | "domain";

export function normalizeSuppression(type: SuppressionType, raw: string): string {
  const value = raw.trim();
  if (type === "email") return value.toLowerCase();
  if (type === "phone") return (value.startsWith("+") ? "+" : "") + value.replace(/\D/g, "");
  return value.toLowerCase().replace(/^www\./, "");
}

export function suppressionHash(type: SuppressionType, raw: string): string {
  return createHash("sha256").update(normalizeSuppression(type, raw), "utf8").digest("hex");
}

/** "https://www.Gym.co.uk/x" or "gym.co.uk" -> "gym.co.uk" (host without www.) */
export function domainFromInput(raw: string): string {
  let v = raw.trim().toLowerCase();
  if (!/^[a-z][a-z0-9+.-]*:\/\//.test(v)) v = `https://${v}`;
  try {
    return new URL(v).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

export async function loadHashes(ownerId: Types.ObjectId): Promise<Set<string>> {
  const rows = await Suppression.find({ ownerId }, { type: 1, valueHash: 1 }).lean();
  return new Set(rows.map((r) => `${r.type}|${r.valueHash}`));
}

type LeadContacts = {
  emails?: { value?: string | null }[] | null;
  phones?: { raw?: string | null; e164?: string | null }[] | null;
  whatsapp?: { e164?: string | null }[] | null;
  website?: { domain?: string | null } | null;
};

/** Same rule as the worker: any email, phone (as written or international) or the domain. */
export function isSuppressed(lead: LeadContacts, hashes: Set<string>): boolean {
  if (hashes.size === 0) return false;
  const candidates: [SuppressionType, string][] = [];
  for (const e of lead.emails ?? []) if (e.value) candidates.push(["email", e.value]);
  for (const p of lead.phones ?? []) {
    if (p.raw) candidates.push(["phone", p.raw]);
    if (p.e164) candidates.push(["phone", p.e164]);
  }
  for (const w of lead.whatsapp ?? []) if (w.e164) candidates.push(["phone", w.e164]);
  if (lead.website?.domain) candidates.push(["domain", lead.website.domain]);
  return candidates.some(([t, v]) => hashes.has(`${t}|${suppressionHash(t, v)}`));
}

/**
 * Opt-out = the business asked not to be contacted, so its leads are removed (decided 2026-11-12).
 * Checks every lead of this owner, so a phone typed as "+44 20 ..." also removes a lead
 * that has it stored as "020 ...". Returns how many leads were deleted.
 */
export async function removeSuppressedLeads(ownerId: Types.ObjectId): Promise<number> {
  const hashes = await loadHashes(ownerId);
  if (hashes.size === 0) return 0;
  const ids: Types.ObjectId[] = [];
  const cursor = Lead.find({ ownerId }, { emails: 1, phones: 1, whatsapp: 1, website: 1 }).lean().cursor();
  for await (const lead of cursor) {
    if (isSuppressed(lead as LeadContacts, hashes)) ids.push(lead._id as Types.ObjectId);
  }
  if (ids.length === 0) return 0;
  const res = await Lead.deleteMany({ ownerId, _id: { $in: ids } });
  return res.deletedCount ?? 0;
}
